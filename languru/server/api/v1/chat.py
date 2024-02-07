import math
import random
import time

import httpx
from fastapi import APIRouter, Body, Request
from openai.types.chat.chat_completion import ChatCompletion
from yarl import URL

from languru.resources.model.discovery import ModelDiscovery
from languru.server.config import settings
from languru.types.chat.completions import ChatCompletionRequest

router = APIRouter()


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    chat_completions_request: ChatCompletionRequest = Body(
        ...,
        example={
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        },
    ),
) -> ChatCompletion:
    if getattr(request.app.state, "model_discovery", None) is None:
        raise ValueError("Model discovery is not initialized")
    model_discovery: "ModelDiscovery" = request.app.state.model_discovery
    models = model_discovery.list(
        id=chat_completions_request.model,
        created_from=math.floor(time.time() - settings.MODEL_REGISTER_PERIOD),
    )
    if len(models) == 0:
        raise ValueError(f"Model '{chat_completions_request.model}' not found")

    model = random.choice(models)
    url = URL(model.owned_by).with_path("/chat/completions")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            str(url), json=chat_completions_request, headers=request.headers
        )
        response.raise_for_status()
        return ChatCompletion(**response.json())
