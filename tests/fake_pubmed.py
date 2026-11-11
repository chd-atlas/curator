"""A fake PubMed for the CLI tests: esearch, esummary and efetch over a dict of papers.

Ported from ``FakePubMed`` in the atlas's ``tests/unit/test_cli_literature.py`` at
b4c8191. Every URL asked for is recorded, so a test can assert that none was.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse


class FakePubMed:
    """esearch / esummary / efetch over a dict of papers, every URL recorded."""

    def __init__(self, papers: dict[str, dict[str, object]]) -> None:
        self.papers = papers
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        endpoint = parsed.path.rsplit("/", 1)[1]
        if endpoint == "esearch.fcgi":
            ids = list(self.papers)
            start, size = int(query["retstart"][0]), int(query["retmax"][0])
            return json.dumps(
                {
                    "esearchresult": {
                        "count": str(len(ids)),
                        "idlist": ids[start : start + size],
                    }
                }
            ).encode()
        wanted = query["id"][0].split(",")
        if endpoint == "esummary.fcgi":
            result: dict[str, object] = {"uids": [p for p in wanted if p in self.papers]}
            for pmid in wanted:
                if pmid in self.papers:
                    paper = self.papers[pmid]
                    result[pmid] = {
                        "uid": pmid,
                        "pubdate": f"{paper['year']} Jan",
                        "title": paper["title"],
                        "fulljournalname": "Journal of Tests",
                        "authors": [{"name": "Zed Z", "authtype": "Author"}],
                        "articleids": [{"idtype": "doi", "value": f"10.1000/t{pmid}"}],
                    }
            return json.dumps({"result": result}).encode()
        articles = ""
        for pmid in wanted:
            if pmid in self.papers:
                paper = self.papers[pmid]
                abstract = (
                    f"<Abstract><AbstractText>{paper['abstract']}</AbstractText></Abstract>"
                    if paper.get("abstract")
                    else ""
                )
                articles += (
                    f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>"
                    f"<ArticleTitle>{paper['title']}</ArticleTitle>{abstract}"
                    f"</Article></MedlineCitation></PubmedArticle>"
                )
        return f"<PubmedArticleSet>{articles}</PubmedArticleSet>".encode()


def papers(*pmids: str) -> dict[str, dict[str, object]]:
    return {
        p: {"title": f"Title of {p}", "year": 2000 + int(p) % 20, "abstract": f"Abstract of {p}."}
        for p in pmids
    }
