import math
import random
import time
from typing import cast

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from openai.types import CreateEmbeddingResponse
from pyassorted.asyncio.executor import run_func
from yarl import URL

from languru.exceptions import ModelNotFound
from languru.server.config import (
    AgentSettings,
    AppType,
    LlmSettings,
    ServerBaseSettings,
)
from languru.server.deps.common import app_settings
from languru.server.utils.common import get_value_from_app
from languru.types.embeddings import EmbeddingRequest

router = APIRouter()


class EmbeddingHandler:
    async def handle_request(
        self,
        request: "Request",
        embedding_request: "EmbeddingRequest",
        settings: "ServerBaseSettings",
        **kwargs,
    ) -> "CreateEmbeddingResponse":
        if settings.APP_TYPE == AppType.llm:
            settings = cast(LlmSettings, settings)
            return await self.handle_llm(
                request=request,
                embedding_request=embedding_request,
                settings=settings,
                **kwargs,
            )

        if settings.APP_TYPE == AppType.agent:
            settings = cast(AgentSettings, settings)
            return await self.handle_agent(
                request=request,
                embedding_request=embedding_request,
                settings=settings,
                **kwargs,
            )

        # Not implemented or unknown app server type
        raise HTTPException(
            status_code=500,
            detail=(
                f"Unknown app server type: {settings.APP_TYPE}"
                if settings.APP_TYPE
                else "App server type not implemented"
            ),
        )

    async def handle_llm(
        self,
        request: "Request",
        embedding_request: "EmbeddingRequest",
        settings: "LlmSettings",
        **kwargs,
    ) -> "CreateEmbeddingResponse":
        from languru.action.base import ActionBase

        action: "ActionBase" = get_value_from_app(
            request.app, key="action", value_typing=ActionBase
        )
        try:
            embedding_request.model = action.get_model_name(embedding_request.model)
        except ModelNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))

        embedding = await run_func(
            action.embeddings, **embedding_request.model_dump(exclude_none=True)
        )
        return embedding

    async def handle_agent(
        self,
        request: "Request",
        embedding_request: "EmbeddingRequest",
        settings: "AgentSettings",
        **kwargs,
    ) -> "CreateEmbeddingResponse":
        from languru.resources.model.discovery import ModelDiscovery

        model_discovery: "ModelDiscovery" = get_value_from_app(
            request.app, key="model_discovery", value_typing=ModelDiscovery
        )
        models = await run_func(
            model_discovery.list,
            id=embedding_request.model,
            created_from=math.floor(time.time() - settings.MODEL_REGISTER_PERIOD),
        )
        if len(models) == 0:
            raise HTTPException(
                status_code=404, detail=f"Model '{embedding_request.model}' not found"
            )

        model = random.choice(models)
        url = URL(model.owned_by).with_path("/embeddings")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                str(url), json=embedding_request.model_dump(exclude_none=True)
            )
            response.raise_for_status()
            return CreateEmbeddingResponse(**response.json())


@router.post("/embeddings")
async def text_completions(
    request: Request,
    embedding_request: EmbeddingRequest = Body(
        ...,
        example={
            "input": "The food was delicious and the waiter...",
            "model": "text-embedding-ada-002",
            "encoding_format": "float",
        },
    ),
    settings: ServerBaseSettings = Depends(app_settings),
) -> CreateEmbeddingResponse:
    return await EmbeddingHandler().handle_request(
        request=request,
        embedding_request=embedding_request,
        settings=settings,
    )
