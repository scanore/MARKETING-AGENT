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

app = FastAPI(title="Agente de Marketing - Novalyze")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN")

# Actor IDs
TIKTOK_ACTOR = "clockworks/free-tiktok-scraper"
INSTAGRAM_ACTOR = "apify/instagram-scraper"


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
    """Run an Apify actor and return dataset items."""
    base = "https://api.apify.com/v2"
    headers = {"Content-Type": "application/json"}
    params = {"token": APIFY_TOKEN}

    async with httpx.AsyncClient(timeout=120) as http:
        # Start the run
        run_resp = await http.post(
            f"{base}/acts/{actor_id}/runs",
            params=params,
            json=input_data,
        )
        if run_resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Apify error: {run_resp.text}")

        run_id = run_resp.json()["data"]["id"]

        # Poll until finished
        for _ in range(30):
            await asyncio.sleep(4)
            status_resp = await http.get(f"{base}/acts/{actor_id}/runs/{run_id}", params=params)
            status = status_resp.json()["data"]["status"]
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise HTTPException(status_code=502, detail=f"Apify run {status}")

        # Fetch dataset
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
            "resultsPerPage": min(quantity * 5, 100),
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
        },
        max_items=quantity * 5,
    )


async def fetch_instagram_data(niche: str, quantity: int) -> list:
    hashtag = niche.strip().lstrip("#").replace(" ", "")
    return await run_apify_actor(
        INSTAGRAM_ACTOR,
        {
            "hashtags": [hashtag],
            "resultsType": "posts",
            "resultsLimit": min(quantity * 5, 100),
            "addParentData": True,
        },
        max_items=quantity * 5,
    )


def normalize_tiktok(items: list) -> list:
    """Convert TikTok Apify items to a unified format."""
    normalized = []
    for item in items:
        author = item.get("authorMeta", {})
        video = item.get("videoMeta", {})
        normalized.append({
            "platform": "TikTok",
            "username": author.get("name", ""),
            "display_name": author.get("nickName", author.get("name", "")),
            "followers": author.get("fans", 0),
            "text": item.get("text", ""),
            "plays": item.get("playCount", 0),
            "likes": item.get("diggCount", 0),
            "shares": item.get("shareCount", 0),
            "comments": item.get("commentCount", 0),
            "duration": video.get("duration", 0),
            "profile_url": f"https://tiktok.com/@{author.get('name', '')}",
        })
    return normalized


def normalize_instagram(items: list) -> list:
    """Convert Instagram Apify items to a unified format."""
    normalized = []
    for item in items:
        owner = item.get("ownerUsername", "") or item.get("owner", {}).get("username", "")
        normalized.append({
            "platform": "Instagram",
            "username": owner,
            "display_name": owner,
            "followers": item.get("ownerFollowersCount", 0),
            "text": item.get("caption", "") or item.get("alt", ""),
            "plays": item.get("videoViewCount", item.get("likesCount", 0)),
            "likes": item.get("likesCount", 0),
            "shares": 0,
            "comments": item.get("commentsCount", 0),
            "duration": item.get("videoDuration", 0),
            "profile_url": f"https://instagram.com/{owner}",
        })
    return normalized


# ─── Claude analysis ──────────────────────────────────────────────────────────

def analyze_with_claude(normalized_items: list, filters: SearchFilters) -> SearchResponse:
    """Send real scraped data to Claude to filter and format influencers."""
    data_str = json.dumps(normalized_items[:60], ensure_ascii=False)

    prompt = f"""You are an influencer analyst. Below is REAL scraped data from {filters.platform} posts about the niche "{filters.niche}".

REAL DATA:
{data_str}

FILTERS TO APPLY:
- Platform: {filters.platform}
- Country preference: {filters.country}
- Min followers: {filters.min_followers:,}
- Max followers: {filters.max_followers:,}
- Min avg views/plays: {filters.min_views:,}
{f'- Min engagement rate: {filters.min_engagement}%' if filters.min_engagement else ''}
- Quantity needed: {filters.quantity}

TASK:
1. Group posts by username to find unique creators
2. For each creator, calculate average plays/likes across their posts
3. Estimate engagement rate as: (likes + comments) / plays * 100
4. Filter by the criteria above
5. Pick the top {filters.quantity} best matches
6. If fewer than {filters.quantity} match strictly, include the closest matches with a note

Return ONLY a valid JSON object, no markdown, no extra text:
{{
  "influencers": [
    {{
      "name": "Display Name",
      "handle": "@username",
      "platform": "{filters.platform}",
      "niche": "{filters.niche}",
      "estimated_followers": "45K",
      "estimated_views": "12K avg",
      "estimated_engagement": "4.2%",
      "country": "{filters.country}",
      "profile_url": "https://{'tiktok' if filters.platform == 'TikTok' else 'instagram'}.com/@username",
      "content_style": "Brief description based on their post captions",
      "why_good_fit": "Why they match the filters"
    }}
  ],
  "search_summary": "Brief summary: how many creators found, quality of matches, data source note"
}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    full_text = "".join(b.text for b in response.content if hasattr(b, "text") and b.text)

    import re
    clean = re.sub(r"```json\s*", "", full_text.strip())
    clean = re.sub(r"```\s*", "", clean).strip()
    start = clean.find("{")
    end = clean.rfind("}") + 1
    if start < 0 or end <= start:
        raise HTTPException(status_code=500, detail="No JSON in Claude response: " + clean[:200])

    data = json.loads(clean[start:end])
    return SearchResponse(
        influencers=data.get("influencers", []),
        search_summary=data.get("search_summary", "Búsqueda completada."),
        data_source=f"Apify Real Data ({filters.platform})",
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

Return ONLY a valid JSON object in this exact format (no markdown, no extra text):
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
  "search_summary": "Brief summary of the search results and quality of matches found"
}}

CRITICAL: Only include creators you actually found with verifiable web data. Return fewer real creators rather than invented ones. Respond with ONLY the JSON object."""


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/search", response_model=SearchResponse)
async def search_influencers(filters: SearchFilters):
    try:
        # Use Apify for TikTok and Instagram if token is available
        if APIFY_TOKEN and filters.platform in ("TikTok", "Instagram"):
            if filters.platform == "TikTok":
                raw_items = await fetch_tiktok_data(filters.niche, filters.quantity)
                normalized = normalize_tiktok(raw_items)
            else:
                raw_items = await fetch_instagram_data(filters.niche, filters.quantity)
                normalized = normalize_instagram(raw_items)

            if normalized:
                return analyze_with_claude(normalized, filters)
            # Fall through to web search if no data returned

        # Fallback: web search (YouTube or if Apify returned nothing)
        prompt = build_web_search_prompt(filters)
        search_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )

        search_text = "".join(
            b.text for b in search_response.content if hasattr(b, "text") and b.text
        )

        format_prompt = f"""Based on this research about influencers:
{search_text}

Format as JSON with EXACTLY this structure, no other text:
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
      "content_style": "Description of content",
      "why_good_fit": "Why they match"
    }}
  ],
  "search_summary": "Brief summary"
}}
Return ONLY the JSON, nothing else."""

        format_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            messages=[{"role": "user", "content": format_prompt}],
        )

        import re
        full_text = "".join(
            b.text for b in format_response.content if hasattr(b, "text") and b.text
        )
        clean = re.sub(r"```json\s*", "", full_text.strip())
        clean = re.sub(r"```\s*", "", clean).strip()
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start < 0 or end <= start:
            raise HTTPException(status_code=500, detail="No JSON found: " + clean[:200])

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
        "version": "2.0.0",
        "provider": "Apify (TikTok + Instagram) + WebSearch (YouTube)",
        "apify_connected": bool(APIFY_TOKEN),
    }


@app.get("/")
async def root():
    base = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base, "static", "index.html"))


app.mount("/", StaticFiles(directory="static", html=True), name="static")
