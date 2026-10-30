"""Expected repository, argument, and machine-output failures."""


class GitError(Exception):
    """A Git request cannot be completed with the supplied repository or arguments."""
