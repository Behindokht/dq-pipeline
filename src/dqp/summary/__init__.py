"""Layer 5: plain-language summary, numbers only from validated, computed facts."""

from dqp.summary.checker import check_draft, fmt, render
from dqp.summary.facts import Fact, FactPack, build_facts
from dqp.summary.llm import LLMClient, LLMError, make_client
from dqp.summary.template import template_draft
from dqp.summary.writer import identifiers_of, write_summary

__all__ = ["Fact", "FactPack", "LLMClient", "LLMError", "build_facts", "check_draft", "fmt",
           "identifiers_of", "make_client", "render", "template_draft", "write_summary"]
