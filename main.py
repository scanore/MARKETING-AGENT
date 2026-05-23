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

app = FastAPI(title="Agente de Marketing - Novalyze")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

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

def build_search_prompt(filters: SearchFilters) -> str:
    follower_range = f"{filters.min_followers:,} - {filters.max_followers:,}"
    prompt = f"""You are an influencer discovery specialist. Search for {filters.platform} influencers matching these exact criteria:

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
For each influencer found, provide realistic estimated data based on their known public presence.

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

Find {filters.quantity} REAL creators. You MUST respond with ONLY the JSON object, no explanations, no text before or after. Start your response with {{ and end with }}."""
    return prompt

@app.post("/search", response_model=SearchResponse)
async def search_influencers(filters: SearchFilters):
    try:
        prompt = build_search_prompt(filters)
        
        # Step 1: Search with web search tool
        search_response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )
        
        search_text = ""
        for block in search_response.content:
            if hasattr(block, "text") and block.text is not None:
                search_text += block.text
        
        # Step 2: Format as JSON
        format_prompt = f"""Based on this research about influencers:

{search_text}

Now format this as a JSON object with EXACTLY this structure, no other text:
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
            model="claude-opus-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": format_prompt}]
        )
        
        full_text = ""
        for block in format_response.content:
            if hasattr(block, "text") and block.text is not None:
                full_text += block.text
        
        import re
        clean = full_text.strip()
        clean = re.sub(r"```json\s*", "", clean)
        clean = re.sub(r"```\s*", "", clean)
        clean = clean.strip()
        
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start >= 0 and end > start:
            clean = clean[start:end]
        else:
            raise HTTPException(status_code=500, detail="No JSON found: " + clean[:200])
        
        data = json.loads(clean)
        
        return SearchResponse(
            influencers=data.get("influencers", []),
            search_summary=data.get("search_summary", "Búsqueda completada."),
            data_source="Web Search (Opción A)"
        )
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Error parsing AI response: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/export")
async def export_influencers(filters: SearchFilters):
    result = await search_influencers(filters)
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "name", "handle", "platform", "niche", "estimated_followers",
        "estimated_views", "estimated_engagement", "country",
        "profile_url", "content_style", "why_good_fit"
    ])
    writer.writeheader()
    for inf in result.influencers:
        writer.writerow(inf.dict())
    
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=influencers_novalyze.csv"}
    )

@app.get("/")
async def root():
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base, "static", "index.html"))

@app.get("/health")
async def health():
    return {"status": "ok", "agent": "marketing", "version": "1.0.0", "provider": "WebSearch"}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
