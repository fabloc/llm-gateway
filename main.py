import logging
import yaml
import re
from typing import List, Dict, Optional, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException, status
from pydantic import BaseModel, ValidationError
import httpx

# --- logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ProxyServer")

class ModelInfos:
    project: str
    location: str
    provider: str
    id: str
    # Define the pattern with named capture groups (?P<name>...)
    # This pattern looks for the literal keys and captures the values following them
    pattern = (
        r"^v[a-z0-9]+"  
        r"/projects/(?P<project>[^/]+)"
        r"/locations/(?P<location>[^/]+)"
        r"/publishers/(?P<provider>[^/]+)"
        r"/models/(?P<id>[^/:]+)"
    )
    
    def __init__(self, path: str):

        match = re.search(self.pattern, path)

        if match:
            # Extracting variables
            data = match.groupdict()
            project = data['project']
            location = data['location']
            provider = data['provider']
            model_id = data['id']
            
            print(f"Success! Project: {project}, Location: {location}, Provider: {provider}, ID: {model_id}")
        else:
            # Special processing for non-conforming paths
            print("Path does not conform to the expected structure. Initiating fallback logic...")
            # handle_custom_logic(url_path)

# --- Configuration Models (Pydantic) ---

class RetryStrategy(BaseModel):
    attempt_index: int
    target_hostname: str
    extra_headers: Dict[str, str] = {}
    timeout_seconds: float

class ProxyConfig(BaseModel):
    max_total_attempts: int
    retry_strategies: List[RetryStrategy]

    def get_strategy(self, attempt: int, prev_retry_strategy: RetryStrategy = None) -> Optional[RetryStrategy]:
        """Finds the configuration specific to the current attempt index."""
        for strategy in self.retry_strategies:
            if strategy.attempt_index == attempt:
                return strategy
        # Fallback: if config is missing for this specific attempt, use the last defined one
        return prev_retry_strategy if self.retry_strategies else None

# --- Global State ---
# In a real app, this might be injected via dependency injection
app_config: Optional[ProxyConfig] = None
http_client: Optional[httpx.AsyncClient] = None

# --- Lifecycle Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config and start http client on startup; clean up on shutdown."""
    global app_config, http_client
    
    # 1. Load Config
    try:
        with open("config.yaml", "r") as f:
            raw_config = yaml.safe_load(f)
            app_config = ProxyConfig(**raw_config)
            logger.info(f"Configuration loaded. Max attempts: {app_config.max_total_attempts}")
    except (FileNotFoundError, ValidationError) as e:
        logger.critical(f"Failed to load configuration: {e}")
        raise e

    # 2. Start Async Client (Connection Pooling)
    http_client = httpx.AsyncClient()
    
    yield
    
    # 3. Cleanup
    await http_client.aclose()

app = FastAPI(lifespan=lifespan)

# --- Utility Logic ---

def transform_outbound_request(
    original_request: Request,
    path: str,
    strategy: RetryStrategy, 
    body_content: bytes
) -> dict:
    """
    Utility function that transforms the inbound request into an outbound request
    specification based on the configuration logic.
    """
    # 1. Construct new URL
    # We strip the hostname from original and append path to the target hostname
    url = f"{strategy.target_hostname}/{path}"
    if original_request.url.query:
        url += f"?{original_request.url.query}"

    # 2. Construct Headers
    # Start with original headers, remove host-specific ones, add config headers
    headers = dict(original_request.headers)
    
    # Remove headers that cause issues when proxying (Host is handled by client)
    headers.pop("host", None)
    headers.pop("content-length", None) 
    
    # Inject business logic headers from config
    headers.update(strategy.extra_headers)

    return {
        "method": original_request.method,
        "url": url,
        "headers": headers,
        "content": body_content,
        "timeout": strategy.timeout_seconds
    }

# --- Main Endpoint ---

@app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def proxy_handler(request: Request, path: str):
    """
    Main entry point. Handles the retry loop logic.
    """

    model_infos = ModelInfos(path)

    # 1. Read body once (cannot stream request body easily in a retry loop 
    # as we need to replay it)
    body = await request.body()
    
    current_attempt = 0
    strategy = None
    last_response: Optional[httpx.Response] = None
    last_exception: Optional[Exception] = None

    # THE RETRY LOOP
    while current_attempt < app_config.max_total_attempts:
        
        # a. Look up transformation logic
        new_strategy = app_config.get_strategy(current_attempt, strategy)
        
        if not new_strategy and not strategy:
            logger.error(f"No strategy found for attempt {current_attempt}. Aborting.")
            break
        
        if new_strategy:
            strategy = new_strategy

        # b. Transform Request
        req_params = transform_outbound_request(request, path, strategy, body)
        
        logger.info(
            f"Attempt {current_attempt + 1}/{app_config.max_total_attempts}: "
            f"Routing to {req_params['url']} with timeout {req_params['timeout']}s"
        )

        try:
            # c. Execute Request
            response = await http_client.request(**req_params)
            
            # d. Success Condition (Business Logic)
            # We assume 429 and 5xx errors trigger a retry, but 2xx/3xx and other 4xx are valid responses.
            # You can adjust this logic (e.g., retry on 429 Too Many Requests).
            if response.status_code < 500 and response.status_code != 429:
                logger.info(f"Success: Received {response.status_code}")
                
                # Stream response back to client (performance best practice)
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    # Filter hop-by-hop headers if necessary
                )
            
            # If 429 and 5xx, we store it and loop again
            logger.warning(f"Upstream returned {response.status_code}. Retrying...")
            last_response = response

        except httpx.RequestError as exc:
            logger.warning(f"Connection error: {exc}. Retrying...")
            last_exception = exc
        
        current_attempt += 1

    # END LOOP
    
    # If we are here, we exhausted retries. Return the last error state.
    logger.error("Max retries exhausted.")

    # If the last response from downstream was a 5xx, propagate that status.
    if last_response and 500 <= last_response.status_code < 600:
        return Response(status_code=last_response.status_code)

    # For all other exhausted retry scenarios, return 429.
    return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)

# To run: uvicorn main:app --reload