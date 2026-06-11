from abc import ABC, abstractmethod
import subprocess


class AgentRunner(ABC):
    """Agent 执行器抽象基类。实现 execute() 即可接入新的 LLM Agent。"""

    @abstractmethod
    def execute(
        self,
        prompt: str,
        timeout: int = 600,
        cwd: str = ".",
        continue_session: bool = False,
    ) -> str:
        """执行 prompt，返回输出。超时或失败时抛异常。"""
        ...
