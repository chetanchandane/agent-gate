"""Single responsibility: the shared exception hierarchy for agent-gate."""


class AgentGateError(Exception):
    """Base class for every error raised inside agent-gate."""


class BudgetExceeded(AgentGateError):
    """Raised when the running LLM spend total passes MAX_SPEND_USD."""


class PolicyGuardViolation(AgentGateError):
    """Raised when a candidate policy violates a hard limit in the policy guard."""


class InvalidProposal(AgentGateError):
    """Raised when an improver proposal cannot be parsed or applied."""


class ConfigError(AgentGateError):
    """Raised when a config or data file is missing or fails validation."""
