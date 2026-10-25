from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .model import ModelError
from .retrieval import RetrievalError
from .storage import Conflict, NotFound
from .workflow import Engine


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)
    method: Literal["keyword", "vector", "hybrid"] = "keyword"

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=2000)
    method: Literal["keyword", "vector", "hybrid"] = "keyword"
    live: bool = False
    strategy: Literal["rag", "agent"] = "rag"

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    method: Literal["keyword", "vector", "hybrid"] | None = None
    live: bool = False
    strategy: Literal["rag", "agent"] | None = None

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("问题不能为空")
        return value.strip() if value is not None else None


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    approved: bool


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    simulate_timeout: bool = False


class FollowupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=2000)
    method: Literal["keyword", "vector", "hybrid"] | None = None
    live: bool = False
    strategy: Literal["rag", "agent"] | None = None

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("追问不能为空")
        return value.strip()


class EditInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=6000)

    @field_validator("answer")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("回复不能为空")
        return value.strip()


def create_app(settings: Settings | None = None, *, engine: Engine | None = None):
    settings = settings or (engine.settings if engine else Settings())
    owned = engine is None
    service = engine or Engine(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned:
            service.close()

    app = FastAPI(title="DocFlow · 技术文档与工单 Agent", version="0.2.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.engine = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # No CORS. A remote page cannot send this custom header without preflight.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if request.headers.get("X-DocFlow-Local") != "1":
                return JSONResponse({"detail": "本地写操作需要X-DocFlow-Local: 1"}, status_code=403)
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "不接受跨源写操作"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    for exception, code in [(Conflict, 409), (NotFound, 404), (RetrievalError, 503), (ModelError, 502), (TimeoutError, 504)]:
        async def handler(request, exc, status=code):
            return JSONResponse({"detail": str(exc)}, status_code=status)
        app.add_exception_handler(exception, handler)

    web = Path(__file__).resolve().parent / "web"
    app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/")
    def page():
        return FileResponse(web / "index.html")

    @app.get("/docs", include_in_schema=False)
    def api_docs():
        # FastAPI's default Swagger page needs remote CDN scripts/inline code,
        # which our local-only CSP intentionally blocks. Serve offline docs.
        return FileResponse(web / "api.html")

    @app.get("/api/config")
    def config():
        vector = service.retrievers.get("vector")
        return {"model": settings.model, "live_enabled": settings.allow_live,
                "budget": service.budget.summary(), "vector_ready": bool(vector and vector.ready),
                "knowledge_documents": len({c.document_id for c in service.chunks}), "chunks": len(service.chunks),
                "strategies": ["rag", "agent"], "methods": list(service.retrievers), "statistics": service.store.statistics()}

    @app.post("/api/search")
    def search(data: SearchInput):
        return {"results": service.search(data.query, data.method)}

    @app.post("/api/search/compare")
    def compare(data: SearchInput):
        results, errors = {}, {}
        for method in ("keyword", "vector", "hybrid"):
            try:
                results[method] = service.search(data.query, method)
            except (RetrievalError, Conflict) as exc:
                results[method], errors[method] = [], str(exc)
        return {"results": results, "errors": errors}

    @app.get("/api/knowledge")
    def knowledge():
        documents = {}
        for chunk in service.chunks:
            item = documents.setdefault(chunk.document_id, {"document_id": chunk.document_id, "title": chunk.title,
                                         "source": chunk.source, "version": chunk.version, "sections": set(), "chunks": 0})
            item["sections"].add(chunk.section)
            item["chunks"] += 1
        return {"documents": [{**item, "sections": len(item["sections"])} for item in documents.values()], "chunks": len(service.chunks)}

    @app.get("/api/tickets")
    def listing(status: str | None = None, q: str | None = None, limit: int = 50):
        return service.store.list_tickets(status, q, limit)

    @app.post("/api/tickets", status_code=201)
    def create(data: TicketInput):
        return service.create(data.question, data.method, data.live, data.strategy)

    @app.post("/api/jobs", status_code=202)
    def enqueue(data: TicketInput):
        return service.enqueue(data.question, data.method, data.live, data.strategy)

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        return service.store.get_job(jid)

    @app.post("/api/jobs/{jid}/retry")
    def retry_job(jid: str):
        return service.retry_job(jid)

    @app.get("/api/tickets/{tid}")
    def get(tid: str):
        return service.store.get(tid)

    @app.post("/api/tickets/{tid}/draft")
    def draft(tid: str, data: DraftInput):
        return service.regenerate(tid, data.question, data.method, data.live, data.strategy)

    @app.post("/api/tickets/{tid}/followups")
    def followup(tid: str, data: FollowupInput):
        return service.followup(tid, data.message, data.method, data.live, data.strategy)

    @app.post("/api/tickets/{tid}/edit")
    def edit(tid: str, data: EditInput):
        return service.edit(tid, data.version, data.answer)

    @app.post("/api/tickets/{tid}/review")
    def review(tid: str, data: ReviewInput):
        return service.review(tid, data.version, data.approved)

    @app.post("/api/tickets/{tid}/submit")
    def submit(tid: str, data: SubmitInput):
        return service.submit(tid, data.version, data.simulate_timeout)

    @app.post("/api/tickets/{tid}/retry")
    def retry(tid: str):
        return service.retry(tid)

    return app
