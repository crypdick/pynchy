"""Semantic GitHub pull-request references shared by trusted integrations."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GitHubPullRequestRef:
    """A canonical github.com pull-request identity."""

    repository: str
    number: int

    @classmethod
    def parse(cls, value: str) -> GitHubPullRequestRef:
        """Parse one canonical GitHub pull-request URL at the untyped boundary."""
        parsed = urlsplit(value.strip())
        path = parsed.path.rstrip("/").split("/")
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "github.com"
            or parsed.query
            or parsed.fragment
            or len(path) != 5
            or path[0]
            or not path[1]
            or not path[2]
            or path[3] != "pull"
            or not path[4].isdigit()
            or int(path[4]) < 1
        ):
            raise ValueError(
                "pull_request_url must be https://github.com/<owner>/<repository>/pull/<number>"
            )
        return cls(repository=f"{path[1]}/{path[2]}", number=int(path[4]))

    @classmethod
    def from_repository_number(cls, repository: str, number: int) -> GitHubPullRequestRef:
        """Construct a reference from a validated webhook route and payload number."""
        return cls.parse(f"https://github.com/{repository}/pull/{number}")

    @property
    def url(self) -> str:
        """Return the canonical URL used as the durable execution link."""
        return f"https://github.com/{self.repository}/pull/{self.number}"
