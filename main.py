import logging
import yaml
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

# --- Configuration Models (Pydantic) ---

class RetryStrategy(BaseModel):
    attempt_index: int
    target_hostname: str
    extra_headers: Dict[str, str] = {}
    timeout_seconds: float

class ProxyConfig(BaseModel):
    max_total_attempts: int
    retry_strategies: List[RetryStrategy]

    def get_strategy(self, attempt: int) -> Optional[RetryStrategy]:
        """Finds the configuration specific to the current attempt index."""
        for strategy in self.retry_strategies:
            if strategy.attempt_index == attempt:
                return strategy
        # Fallback: if config is missing for this specific attempt, use the last defined one
        return self.retry_strategies[-1] if self.retry_strategies else None

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
    strategy: RetryStrategy, 
    body_content: bytes
) -> dict:
    """
    Utility function that transforms the inbound request into an outbound request
    specification based on the configuration logic.
    """
    # 1. Construct new URL
    # We strip the hostname from original and append path to the target hostname
    url = f"{strategy.target_hostname}{original_request.url.path}"
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
    # 1. Read body once (cannot stream request body easily in a retry loop 
    # as we need to replay it)
    body = await request.body()
    
    current_attempt = 0
    last_response: Optional[httpx.Response] = None
    last_exception: Optional[Exception] = None

    # THE RETRY LOOP
    while current_attempt < app_config.max_total_attempts:
        
        # a. Look up transformation logic
        strategy = app_config.get_strategy(current_attempt)
        
        if not strategy:
            logger.error(f"No strategy found for attempt {current_attempt}. Aborting.")
            break

        # b. Transform Request
        req_params = transform_outbound_request(request, strategy, body)
        
        logger.info(
            f"Attempt {current_attempt + 1}/{app_config.max_total_attempts}: "
            f"Routing to {req_params['url']} with timeout {req_params['timeout']}s"
        )

        try:
            # c. Execute Request
            response = await http_client.request(**req_params)
            
            # d. Success Condition (Business Logic)
            # We assume 5xx errors trigger a retry, but 2xx/3xx/4xx are valid responses.
            # You can adjust this logic (e.g., retry on 429 Too Many Requests).
            if response.status_code < 500:
                logger.info(f"Success: Received {response.status_code}")
                
                # Stream response back to client (performance best practice)
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    # Filter hop-by-hop headers if necessary
                )
            
            # If 5xx, we store it and loop again
            logger.warning(f"Upstream returned {response.status_code}. Retrying...")
            last_response = response

        except httpx.RequestError as exc:
            logger.warning(f"Connection error: {exc}. Retrying...")
            last_exception = exc
        
        current_attempt += 1

    # END LOOP
    
    # If we are here, we exhausted retries. Return the last error state.
    logger.error("Max retries exhausted.")

    if last_response:
        return Response(
            content=last_response.content,
            status_code=last_response.status_code,
            media_type=last_response.headers.get("content-type")
        )
    
    if last_exception:
        # If we never got a response (network down), return 502 Bad Gateway
        return Response(
            content=f"Upstream unreachable: {str(last_exception)}",
            status_code=status.HTTP_502_BAD_GATEWAY
        )

    return Response(content="Configuration Error", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

# To run: uvicorn main:app --reload