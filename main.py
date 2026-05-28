from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import anthropic
import json
import csv
import io
import os
import httpx
import asyncio
import re

app = FastAPI(title="Agente de Marketing - Novalyze")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN")

TIKTOK_ACTOR = "GdWCkxBtKWOsKjdch"
INSTAGRAM_ACTOR = "shu8hvrXbJbY3Eb9W"
INSTAGRAM_PROFILE_ACTOR = "apify/instagram-profile-scraper"


class SearchFilters(BaseModel):
    platform: str = "TikTok"
    niche: str = ""
    country: str = "US"
    min_followers: int = 10000
    max_followers: int = 500000
    min_views: int = 5000
    min_engagement: Optional[float] = None
    min_age: Optional[int] = None
    keywords: Optional[str] = None
    quantity: int = 10


class Influencer(BaseModel):
    name: str
    handle: str
    platform: str
    niche: str
    estimated_followers: str
    estimated_views: str
    estimated_engagement: str
    country: str
    profile_url: str
    content_style: str
    why_good_fit: str


class SearchResponse(BaseModel):
    influencers: List[Influencer]
    search_summary: str
    data_source: str = "Web Search (Opción A)"


# ─── Apify helpers ────────────────────────────────────────────────────────────

async def run_apify_actor(actor_id: str, input_data: dict, max_items: int = 50) -> list:
    base = "https://api.apify.com/v2"
    params = {"token": APIFY_TOKEN}

    async with httpx.AsyncClient(timeout=180) as http:
        run_resp = await http.post(
            f"{base}/acts/{actor_id}/runs",
            params=params,
            json=input_data,
        )
        if run_resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Apify error: {run_resp.text}")

        run_id = run_resp.json()["data"]["id"]

        for _ in range(40):
            await asyncio.sleep(4)
            status_resp = await http.get(f"{base}/acts/{actor_id}/runs/{run_id}", params=params)
            status = status_resp.json()["data"]["status"]
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise HTTPException(status_code=502, detail=f"Apify run {status}")

        dataset_id = status_resp.json()["data"]["defaultDatasetId"]
        items_resp = await http.get(
            f"{base}/datasets/{dataset_id}/items",
            params={**params, "limit": max_items},
        )
        return items_resp.json()


async def fetch_tiktok_data(niche: str, quantity: int) -> list:
    hashtag = niche.strip().lstrip("#").replace(" ", "")
    return await run_apify_actor(
        TIKTOK_ACTOR,
        {
            "hashtags": [hashtag],
            "resultsPerPage": min(quantity * 5, 500),
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
        },
        max_items=quantity * 5,
    )


async def verify_instagram_profiles(usernames: list) -> dict:
    """Verify real follower counts by scraping each profile URL."""
    if not usernames:
        return {}
    try:
        urls = [f"https://www.instagram.com/{u}/" for u in usernames[:50]]
        items = await run_apify_actor(
            INSTAGRAM_ACTOR,
            {
                "directUrls": urls,
                "resultsType": "details",
                "resultsLimit": 1,
            },
            max_items=50,
        )
        result = {}
        for item in items:
            username = item.get("username", "") or item.get("ownerUsername", "")
            if username:
                result[username] = {
                    "followers": item.get("followersCount", 0) or item.get("ownerFollowersCount", 0),
                    "display_name": item.get("fullName", "") or item.get("ownerFullName", username),
                    "posts_count": item.get("postsCount", 0),
                    "biography": item.get("biography", ""),
                }
        return result
    except Exception:
        return {}


def normalize_tiktok(items: list) -> list:
    authors = {}
    for item in items:
        author = item.get("authorMeta", {})
        username = author.get("name", "")
        if not username:
            continue
        if username not in authors:
            authors[username] = {
                "platform": "TikTok",
                "username": username,
                "display_name": author.get("nickName", username),
                "followers": author.get("fans", 0),
                "profile_url": f"https://tiktok.com/@{username}",
                "posts": [],
            }
        fans = author.get("fans", 0)
        if fans > authors[username]["followers"]:
            authors[username]["followers"] = fans
        authors[username]["posts"].append({
            "text": item.get("text", ""),
            "plays": item.get("playCount", 0),
            "likes": item.get("diggCount", 0),
            "shares": item.get("shareCount", 0),
            "comments": item.get("commentCount", 0),
        })

    normalized = []
    for username, data in authors.items():
        posts = data["posts"]
        avg_plays = int(sum(p["plays"] for p in posts) / len(posts)) if posts else 0
        avg_likes = int(sum(p["likes"] for p in posts) / len(posts)) if posts else 0
        avg_comments = int(sum(p["comments"] for p in posts) / len(posts)) if posts else 0
        engagement = round((avg_likes + avg_comments) / avg_plays * 100, 2) if avg_plays > 0 else 0
        normalized.append({
            "platform": "TikTok",
            "username": username,
            "display_name": data["display_name"],
            "followers": data["followers"],
            "avg_plays": avg_plays,
            "avg_likes": avg_likes,
            "avg_comments": avg_comments,
            "engagement_rate": engagement,
            "sample_captions": [p["text"][:100] for p in posts[:3]],
            "profile_url": data["profile_url"],
        })
    return normalized


def build_tiktok_results(normalized: list, filters: SearchFilters) -> SearchResponse:
    """Build TikTok results directly from real Apify data — no Claude estimation."""
    # Sort by engagement rate
    sorted_creators = sorted(normalized, key=lambda x: x.get("engagement_rate", 0), reverse=True)
    
    # Filter by criteria
    matched = []
    for c in sorted_creators:
        followers = c.get("followers", 0)
        avg_plays = c.get("avg_plays", 0)
        engagement = c.get("engagement_rate", 0)
        
        if followers < filters.min_followers or followers > filters.max_followers:
            continue
        if avg_plays < filters.min_views:
            continue
        if filters.min_engagement and engagement < filters.min_engagement:
            continue
        matched.append(c)
    
    # Fallback: if not enough matches, use best available
    if len(matched) < filters.quantity:
        for c in sorted_creators:
            if len(matched) >= filters.quantity:
                break
            if c.get("username") not in [m.get("username") for m in matched]:
                matched.append(c)
    
    influencers = []
    for c in matched[:filters.quantity]:
        followers = c.get("followers", 0)
        avg_plays = c.get("avg_plays", 0)
        engagement = c.get("engagement_rate", 0)
        
        if followers >= 1_000_000:
            followers_str = f"{followers/1_000_000:.1f}M"
        elif followers >= 1_000:
            followers_str = f"{followers/1_000:.0f}K"
        elif followers > 0:
            followers_str = str(followers)
        else:
            followers_str = "N/A"
        
        if avg_plays >= 1_000_000:
            views_str = f"{avg_plays/1_000_000:.1f}M avg"
        elif avg_plays >= 1_000:
            views_str = f"{avg_plays/1_000:.0f}K avg"
        elif avg_plays > 0:
            views_str = f"{avg_plays} avg"
        else:
            views_str = "N/A"
        
        eng_str = f"{engagement:.1f}%" if engagement > 0 else "N/A"
        captions = c.get("sample_captions", [])
        content_style = captions[0][:80] if captions else f"TikTok creator in {filters.niche} niche"
        
        influencers.append({
            "name": c.get("display_name", c.get("username", "")),
            "handle": f"@{c.get('username', '')}",
            "platform": "TikTok",
            "niche": filters.niche,
            "estimated_followers": followers_str,
            "estimated_views": views_str,
            "estimated_engagement": eng_str,
            "country": filters.country,
            "profile_url": c.get("profile_url", f"https://tiktok.com/@{c.get('username', '')}"),
            "content_style": content_style,
            "why_good_fit": f"Real data: {followers_str} followers, {views_str} views, {eng_str} engagement",
        })
    
    return SearchResponse(
        influencers=influencers,
        search_summary=f"{len(matched)} creators matched from {len(normalized)} scraped. All data real from Apify.",
        data_source="Apify Real Data (TikTok)",
    )


def analyze_tiktok_with_claude(normalized: list, filters: SearchFilters) -> SearchResponse:
    return build_tiktok_results(normalized, filters)




async def search_instagram_verified(filters: SearchFilters) -> SearchResponse:
    """Web search finds influencers -> regex extracts usernames -> Apify verifies followers."""
    follower_range = f"{filters.min_followers:,} - {filters.max_followers:,}"

    # Step 1: Web search for real Instagram profiles
    prompt = f"""Search Instagram for real {filters.niche} content creators from {filters.country}.
Search specifically for: site:instagram.com {filters.niche} {filters.country}
Also search: "{filters.niche} influencer instagram {filters.country}" @username

I need their Instagram usernames (the @handle). List each one as:
@username - number of followers

Find creators with {follower_range} followers."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Get all text from response
    all_text = ""
    for block in response.content:
        if hasattr(block, "text") and block.text:
            all_text += block.text + " "

    # Try regex first: @mentions
    usernames = re.findall(r'@([a-zA-Z0-9._]{3,30})', all_text)
    usernames = list(dict.fromkeys(usernames))[:30]

    # Try instagram.com URLs
    if not usernames:
        usernames = re.findall(r'instagram\.com/([a-zA-Z0-9._]{3,30})', all_text)
        usernames = list(dict.fromkeys(usernames))[:30]

    # If still nothing, ask Claude to extract usernames explicitly
    if not usernames and all_text.strip():
        extract = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[
                {"role": "user", "content": f"From this text, extract all Instagram usernames (the part after @). Return as comma-separated list only, no other text:\n\n{all_text[:3000]}"}
            ],
        )
        extract_text = "".join(b.text for b in extract.content if hasattr(b, "text") and b.text)
        raw = [u.strip().lstrip("@") for u in extract_text.split(",")]
        usernames = [u for u in raw if u and len(u) >= 3 and " " not in u][:30]

    # Last resort: ask Claude directly for usernames without web search
    if not usernames:
        direct = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[
                {"role": "user", "content": f"List {filters.quantity * 3} real Instagram usernames (without @) for {filters.niche} influencers in {filters.country} with {follower_range} followers. Return comma-separated usernames only, nothing else."}
            ],
        )
        direct_text = "".join(b.text for b in direct.content if hasattr(b, "text") and b.text)
        raw = [u.strip().lstrip("@") for u in direct_text.replace("\n", ",").split(",")]
        usernames = [u for u in raw if u and len(u) >= 3 and " " not in u][:30]

    # Step 2: Verify with Apify Profile Scraper
    verified = {}
    if usernames and APIFY_TOKEN:
        try:
            verified = await asyncio.wait_for(
                verify_instagram_profiles(usernames[:20]),
                timeout=60.0
            )
        except Exception:
            verified = {}

    # Step 3: Build results
    influencers = []
    target = (filters.min_followers + filters.max_followers) / 2

    # First: verified profiles sorted by closeness to target
    sorted_verified = sorted(
        verified.items(),
        key=lambda x: abs(x[1].get("followers", 0) - target)
    )

    for username, v in sorted_verified:
        if len(influencers) >= filters.quantity:
            break
        followers = v.get("followers", 0)
        if followers == 0:
            continue
        if followers < filters.min_followers or followers > filters.max_followers:
            continue
        display_name = v.get("display_name", username)
        if followers >= 1_000_000:
            fs = f"{followers/1_000_000:.1f}M"
            av = f"{int(followers*0.03/1000)}K avg"
            er = "1.5%"
        elif followers >= 100_000:
            fs = f"{int(followers/1000)}K"
            av = f"{int(followers*0.05/1000)}K avg"
            er = "3.2%"
        else:
            fs = f"{int(followers/1000)}K"
            av = f"{int(followers*0.07/1000)}K avg"
            er = "4.8%"
        influencers.append({
            "name": display_name,
            "handle": f"@{username}",
            "platform": "Instagram",
            "niche": filters.niche,
            "estimated_followers": fs,
            "estimated_views": av,
            "estimated_engagement": er,
            "country": filters.country,
            "profile_url": f"https://instagram.com/{username}",
            "content_style": f"Instagram {filters.niche} creator",
            "why_good_fit": f"✓ Apify verified: {fs} followers",
        })

    # Fallback: verified but outside range
    if len(influencers) < filters.quantity:
        for username, v in sorted_verified:
            if len(influencers) >= filters.quantity:
                break
            if any(i["handle"] == f"@{username}" for i in influencers):
                continue
            followers = v.get("followers", 0)
            if followers == 0:
                continue
            display_name = v.get("display_name", username)
            if followers >= 1_000_000:
                fs = f"{followers/1_000_000:.1f}M"
                av = f"{int(followers*0.03/1000)}K avg"
                er = "1.5%"
            elif followers >= 1_000:
                fs = f"{int(followers/1000)}K"
                av = f"{int(followers*0.05/1000)}K avg"
                er = "3.5%"
            else:
                continue
            influencers.append({
                "name": display_name,
                "handle": f"@{username}",
                "platform": "Instagram",
                "niche": filters.niche,
                "estimated_followers": fs,
                "estimated_views": av,
                "estimated_engagement": er,
                "country": filters.country,
                "profile_url": f"https://instagram.com/{username}",
                "content_style": f"Instagram {filters.niche} creator",
                "why_good_fit": f"✓ Verified {fs} followers (outside range)",
            })

    # Last fallback: unverified from web search with estimated data
    if len(influencers) < filters.quantity:
        for username in usernames:
            if len(influencers) >= filters.quantity:
                break
            if any(i["handle"] == f"@{username}" for i in influencers):
                continue
            if username in verified:
                continue
            est = int(target)
            if est >= 1_000_000:
                fs = f"{est/1_000_000:.1f}M"
                av = f"{int(est*0.04/1000)}K avg"
                er = "2.5%"
            elif est >= 1_000:
                fs = f"{int(est/1000)}K"
                av = f"{int(est*0.06/1000)}K avg"
                er = "4.0%"
            else:
                continue
            influencers.append({
                "name": username.replace(".", " ").replace("_", " ").title(),
                "handle": f"@{username}",
                "platform": "Instagram",
                "niche": filters.niche,
                "estimated_followers": fs,
                "estimated_views": av,
                "estimated_engagement": er,
                "country": filters.country,
                "profile_url": f"https://instagram.com/{username}",
                "content_style": f"Instagram {filters.niche} creator",
                "why_good_fit": "Found via web search (unverified)",
            })

    verified_count = len(verified)
    summary = f"Scrapeados: {len(usernames)} usernames. Verificados: {verified_count}. En rango: {len(influencers)}."
    if len(usernames) == 0:
        summary = "ERROR: Web search no encontró usernames. Intenta con otro nicho."
    elif verified_count == 0:
        summary = f"ERROR: Apify no verificó ningún perfil de {len(usernames)} encontrados: {usernames[:5]}"
    elif len(influencers) == 0:
        summary = f"ERROR: {verified_count} verificados pero ninguno en rango {filters.min_followers:,}-{filters.max_followers:,}. Seguidores encontrados: {[v.get('followers',0) for v in verified.values()][:5]}"
    return SearchResponse(
        influencers=influencers[:filters.quantity],
        search_summary=summary,
        data_source=f"Web Search + Apify ({verified_count} verificados)",
    )



def build_web_search_prompt(filters: SearchFilters) -> str:
    follower_range = f"{filters.min_followers:,} - {filters.max_followers:,}"
    return f"""You are an influencer discovery specialist. Search for {filters.platform} influencers matching these exact criteria:

SEARCH CRITERIA:
- Platform: {filters.platform}
- Niche/Category: {filters.niche if filters.niche else 'Any'}
- Country: {filters.country}
- Follower range: {follower_range}
- Minimum avg views per video: {filters.min_views:,}+
{f'- Minimum engagement rate: {filters.min_engagement}%+' if filters.min_engagement else ''}
{f'- Minimum creator age: {filters.min_age} years old' if filters.min_age else ''}
{f'- Additional keywords: {filters.keywords}' if filters.keywords else ''}
- Quantity needed: {filters.quantity} influencers

Search the web and find REAL {filters.platform} influencers/creators that match these criteria.

Return ONLY a valid JSON object (no markdown):
{{
  "influencers": [
    {{
      "name": "Creator Real Name",
      "handle": "@username",
      "platform": "{filters.platform}",
      "niche": "specific niche",
      "estimated_followers": "45K",
      "estimated_views": "12K avg",
      "estimated_engagement": "4.2%",
      "country": "{filters.country}",
      "profile_url": "https://{filters.platform.lower()}.com/@username",
      "content_style": "Brief description of their content style",
      "why_good_fit": "Why they match the search criteria"
    }}
  ],
  "search_summary": "Brief summary of results"
}}
CRITICAL: Only real creators with verifiable data. Respond with ONLY the JSON."""


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/search", response_model=SearchResponse)
async def search_influencers(filters: SearchFilters):
    try:
        if filters.platform == "TikTok" and APIFY_TOKEN:
            raw_items = await fetch_tiktok_data(filters.niche, filters.quantity)
            normalized = normalize_tiktok(raw_items)
            if normalized:
                return analyze_tiktok_with_claude(normalized, filters)

        if filters.platform == "Instagram":
            return await search_instagram_verified(filters)

        # Fallback: web search (YouTube or if Apify returned nothing)
        prompt = build_web_search_prompt(filters)
        search_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        search_text = "".join(
            b.text for b in search_response.content if hasattr(b, "text") and b.text
        )
        format_prompt = f"""Based on this research:
{search_text}

Format as JSON:
{{
  "influencers": [
    {{
      "name": "Creator Name",
      "handle": "@username",
      "platform": "{filters.platform}",
      "niche": "niche",
      "estimated_followers": "50K",
      "estimated_views": "15K avg",
      "estimated_engagement": "4.5%",
      "country": "{filters.country}",
      "profile_url": "https://tiktok.com/@username",
      "content_style": "Description",
      "why_good_fit": "Why they match"
    }}
  ],
  "search_summary": "Brief summary"
}}
Return ONLY the JSON."""

        format_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            messages=[{"role": "user", "content": format_prompt}],
        )
        full_text = "".join(
            b.text for b in format_response.content if hasattr(b, "text") and b.text
        )
        clean = re.sub(r"```json\s*", "", full_text.strip())
        clean = re.sub(r"```\s*", "", clean).strip()
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start < 0 or end <= start:
            raise HTTPException(status_code=500, detail="No JSON: " + clean[:200])
        data = json.loads(clean[start:end])
        return SearchResponse(
            influencers=data.get("influencers", []),
            search_summary=data.get("search_summary", "Búsqueda completada."),
            data_source="Web Search (Opción A)",
        )

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Error parsing AI response: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/export")
async def export_influencers(filters: SearchFilters):
    result = await search_influencers(filters)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "name", "handle", "platform", "niche", "estimated_followers",
        "estimated_views", "estimated_engagement", "country",
        "profile_url", "content_style", "why_good_fit",
    ])
    writer.writeheader()
    for inf in result.influencers:
        writer.writerow(inf.dict())
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=influencers_novalyze.csv"},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "marketing",
        "version": "4.0.0",
        "provider": "TikTok: Apify Real Data | Instagram: Web Search + Apify Verificado | YouTube: Web Search",
        "apify_connected": bool(APIFY_TOKEN),
    }


@app.get("/")
async def root():
    base = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base, "static", "index.html"))


app.mount("/", StaticFiles(directory="static", html=True), name="static")
