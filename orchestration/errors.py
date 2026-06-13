class NeedsHumanError(RuntimeError):
    """The run made progress but cannot continue safely without intervention."""


class NoChangesError(NeedsHumanError):
    """The run reached submission without a commit relative to the base branch."""
