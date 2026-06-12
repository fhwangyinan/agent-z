from dataclasses import dataclass


@dataclass
class RuntimeOptions:
    auto_mode: bool = False
    total_loops: int = 0
    current_loop: int = 0
    force_develop: bool = False
    keep_worktree: bool = False


runtime = RuntimeOptions()
