from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


SessionMode = Literal["none", "latest", "explicit"]
OutputMode = Literal["text", "events"]
IsolationMode = Literal["none", "permissions", "sandbox"]


@dataclass(frozen=True)
class BackendCapabilities:
    session_mode: SessionMode
    output_mode: OutputMode
    isolation: IsolationMode
    stdin_prompt: bool


@dataclass(frozen=True)
class AgentResult:
    output: str
    session_id: str | None = None


class AgentRunner(ABC):
    """Common interface for coding-agent CLI backends."""

    name: str
    command: str
    capabilities: BackendCapabilities

    @abstractmethod
    def execute(
        self,
        prompt: str,
        timeout: int = 600,
        cwd: str = ".",
        session_id: str | None = None,
    ) -> AgentResult:
        """Execute a prompt and return the final output and resumable session ID."""
        ...
