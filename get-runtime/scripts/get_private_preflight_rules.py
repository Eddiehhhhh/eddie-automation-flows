"""Validate the current private-vault GitHub task routes without logging content."""

ROUTING_INDEX = 'Wiki/06 Systems/01 Routing/规则入口索引.md'
GITHUB_ENTRY = 'Wiki/06 Systems/01 Routing/GitHub AI 接入入口.md'
GITHUB_OPERATIONS = 'Wiki/06 Systems/02 Operations/GitHub 自动化与发布经验.md'
CLOUD_INBOX = 'Raw/00 Meta/Cloud Inbox/README.md'
AGENT_RULES = 'AGENTS.md'


def valid_github_route(documents):
    """Require the actual route row and both canonical rule links."""
    required = {ROUTING_INDEX, GITHUB_ENTRY, GITHUB_OPERATIONS, CLOUD_INBOX, AGENT_RULES}
    if not isinstance(documents, dict) or not required <= set(documents):
        return False
    if any(not isinstance(documents[path], str) or not documents[path].strip() for path in required):
        return False
    route = documents[ROUTING_INDEX]
    agents = documents[AGENT_RULES]
    entry = documents[GITHUB_ENTRY]
    return (
        '| GitHub、Actions、PR、push、secret、deploy |' in route
        and GITHUB_OPERATIONS in route
        and GITHUB_ENTRY in agents
        and GITHUB_OPERATIONS in agents
        and 'Raw/00 Meta/Cloud Inbox/README.md' in entry
    )
