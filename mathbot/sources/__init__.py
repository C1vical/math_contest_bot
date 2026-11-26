"""Source adapters, one per provider."""

from __future__ import annotations

from mathbot.contests import AOPS_COMMUNITY, AOPS_WIKI, CEMC, CMS, HMMT
from mathbot.http import PoliteClient
from mathbot.sources.aops_community import AopsCommunityAdapter
from mathbot.sources.aops_wiki import AopsWikiAdapter
from mathbot.sources.base import SourceAdapter
from mathbot.sources.cemc import CemcAdapter
from mathbot.sources.cms import CmsAdapter
from mathbot.sources.hmmt import HmmtAdapter


def build_adapters(client: PoliteClient, *, refresh: bool = False) -> dict[str, SourceAdapter]:
    return {
        AOPS_WIKI: AopsWikiAdapter(client, refresh=refresh),
        AOPS_COMMUNITY: AopsCommunityAdapter(client, refresh=refresh),
        CEMC: CemcAdapter(client, refresh=refresh),
        CMS: CmsAdapter(client, refresh=refresh),
        HMMT: HmmtAdapter(client, refresh=refresh),
    }


__all__ = ["SourceAdapter", "build_adapters"]
