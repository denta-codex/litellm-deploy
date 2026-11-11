"""Consistent native reasoning replay for the pinned Chat/Responses bridge."""
import copy
from importlib.metadata import version

from responses_events import normalize_item
from litellm.types.utils import BaseLiteLLMOpenAIResponseObject


def plain(value):
    return value.model_dump(exclude_none=True) if hasattr(value, 'model_dump') else copy.deepcopy(value)


def replay_item(value):
    item = normalize_item(plain(value))
    if item['type'] == 'reasoning':
        # Raw Chat reasoning is replay state, not a provider-authored summary.
        item['summary'] = []
        item.pop('status', None)
    return item


def install():
    """Pinned presentation fix; retain the provider's native replay conversion.

    Remove this hook when upstream passes test_kimi_bridge/test_fable_bridge
    without it, including signed replay and strict streamed item lifecycles.
    """
    if version('litellm') != '1.106.5':
        raise RuntimeError('Native Chat bridge requires LiteLLM 1.106.5; revalidate before upgrading')
    from litellm.responses.litellm_completion_transformation.streaming_iterator import LiteLLMCompletionStreamingIterator as Iterator
    from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig as Transform
    from litellm.proxy import common_request_processing
    original_terminal = Iterator._emit_terminal_response_event
    original_reasoning = Iterator.create_reasoning_output_item_done_event
    original_anext = Iterator.__anext__
    original_next = Iterator.__next__
    original_transform = Transform.transform_chat_completion_response_to_responses_api_response
    def terminal(self, response):
        event = original_terminal(self, response)
        if event is not None:
            output = [replay_item(item) for item in plain(event.response).get('output', [])]
            done = getattr(self, '_bridge_done_items', {})
            for item in output:
                prior = done.get(item['id'])
                if prior is not None:
                    if item['type'] == 'reasoning' and item != prior:
                        raise ValueError('Completed reasoning differs from streamed replay state')
                    # The emitted done envelope owns public presentation fields.
                    item.clear(); item.update(copy.deepcopy(prior))
            event.response.output = output
        return event
    def reasoning_done(self, *args, **kwargs):
        event = original_reasoning(self, *args, **kwargs)
        # Build once at the reasoning boundary, not on every thinking delta.
        # The stock converter preserves empty signed and redacted blocks too.
        snapshot = self.create_litellm_model_response()
        items = Transform._extract_reasoning_output_items(snapshot, snapshot.choices) if snapshot else []
        item = replay_item(items[0] if items else event.item)
        item['id'] = event.item.id
        event.item = BaseLiteLLMOpenAIResponseObject(**item)
        return event
    def transform(*args, **kwargs):
        response = original_transform(*args, **kwargs)
        # Upstream ID alignment uses attributes and model_copy. Plain dicts here
        # break that alignment (and can make deltas reference an unknown item).
        response.output = [BaseLiteLLMOpenAIResponseObject(**replay_item(item))
                           for item in plain(response).get('output', [])]
        return response

    def present(self, event):
        if event.type.startswith('response.reasoning_summary'):
            # Suppress part/done events as well as deltas; raw signed or readable
            # thinking is not a summary. The replay item is still emitted.
            return None
        if event.type in ('response.output_item.added', 'response.output_item.done'):
            event.item = BaseLiteLLMOpenAIResponseObject(**replay_item(event.item))
            if event.type == 'response.output_item.done':
                if not hasattr(self, '_bridge_done_items'):
                    self._bridge_done_items = {}
                self._bridge_done_items[event.item.id] = plain(event.item)
        # Some upstream done/terminal events omit sequence numbers or hardcode
        # 1. Number at emission, after filtering, rather than queue creation.
        sequence = getattr(self, '_bridge_sequence_number', 0)
        event.sequence_number = sequence
        self._bridge_sequence_number = sequence + 1
        return event

    async def anext(self):
        while True:
            event = present(self, await original_anext(self))
            if event is not None:
                return event

    def next_event(self):
        while True:
            event = present(self, original_next(self))
            if event is not None:
                return event

    Iterator.__anext__ = anext
    Iterator.__next__ = next_event
    Iterator._emit_terminal_response_event = terminal
    Iterator.create_reasoning_output_item_done_event = reasoning_done
    Transform.transform_chat_completion_response_to_responses_api_response = staticmethod(transform)
    integration = common_request_processing.route_request
    def uninstall():
        Iterator._emit_terminal_response_event = original_terminal
        Iterator.create_reasoning_output_item_done_event = original_reasoning
        Iterator.__anext__ = original_anext
        Iterator.__next__ = original_next
        Transform.transform_chat_completion_response_to_responses_api_response = original_transform
    integration.uninstall = uninstall
    return integration
