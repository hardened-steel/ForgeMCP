"""Expected repository, argument, and machine-output failures."""


class GitError(Exception):
    """A Git request cannot be completed with the supplied repository or arguments."""


class GitParseError(GitError):
    """Git returned machine output that cannot be interpreted safely."""
