from dataclasses import dataclass
from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / ".runtime"
    allow_live: bool = False
    model: str = "deepseek-flash"

    @property
    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    @property
    def vector_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def budget_path(self) -> Path:
        # All normal CLI/server runs share one ledger, even with another output/data directory.
        return self.root / ".runtime" / "model-budget.sqlite3"

    @property
    def model_path(self) -> str | None:
        configured = os.environ.get("DOCFLOW_EMBEDDING_MODEL_PATH")
        if configured:
            return configured
        downloaded = self.root / ".runtime" / "models" / "bge-small-zh-v1.5"
        return str(downloaded) if downloaded.is_dir() else None
