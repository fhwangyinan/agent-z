class NeedsHumanError(RuntimeError):
    """The run made progress but cannot continue safely without intervention."""


class NoChangesError(NeedsHumanError):
    """The run reached submission without a commit relative to the base branch."""


class WorkspaceIsolationError(NeedsHumanError):
    """An agent changed protected repository state outside its task worktree."""
