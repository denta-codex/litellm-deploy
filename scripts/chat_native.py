"""Consistent native reasoning replay for the pinned Chat/Responses bridge."""
import copy
from importlib.metadata import version

from responses_events import normalize_item
from litellm.types.utils import BaseLiteLLMOpenAIResponseObject


def plain(value):
    return value.model_dump(exclude_none=True) if hasattr(value, 'model_dump') else copy.deepcopy(value)


def install():
    """Keep terminal reasoning consistent without changing provider replay."""
    if version('litellm') != '1.106.5':
        raise RuntimeError('Native Chat bridge requires LiteLLM 1.106.5; revalidate before upgrading')
    from litellm.responses.litellm_completion_transformation.streaming_iterator import LiteLLMCompletionStreamingIterator as Iterator
    from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig as Transform
    from litellm.proxy import common_request_processing
    original_terminal = Iterator._emit_terminal_response_event
    original_reasoning = Iterator.create_reasoning_output_item_done_event
    original_delta = Iterator._transform_chat_completion_chunk_to_response_api_chunk
    original_transform = Transform.transform_chat_completion_response_to_responses_api_response
    def terminal(self, response):
        event = original_terminal(self, response)
        if event is not None:
            output = [normalize_item(item) for item in plain(event.response).get('output', [])]
            message_done = next((plain(i) for i in self._pending_response_events
                                 if getattr(i, 'type', None) == 'response.output_item.done'), None)
            for item in output:
                if item['type'] == 'reasoning' and self._cached_reasoning_item_id:
                    item['id'] = self._cached_reasoning_item_id
                    item['content'] = [{'type': 'output_text', 'text': ''.join(self._accumulated_reasoning_content_parts),
                                        'annotations': []}]
                    item.pop('summary', None)
                    item.pop('status', None)
                    item.pop('encrypted_content', None)
                elif item['type'] == 'message' and self._cached_item_id:
                    if message_done is not None:
                        item.clear(); item.update(message_done['item'])
                    else:
                        item['id'] = self._cached_item_id
            event.response.output = output
        return event
    def reasoning_done(self, *args, **kwargs):
        event = original_reasoning(self, *args, **kwargs)
        item = normalize_item(plain(event.item))
        item['content'] = [{'type': 'output_text', 'text': ''.join(self._accumulated_reasoning_content_parts),
                            'annotations': []}]
        item.pop('summary', None)
        item.pop('status', None)
        event.item = BaseLiteLLMOpenAIResponseObject(**item)
        return event
    def transform(*args, **kwargs):
        response = original_transform(*args, **kwargs)
        response.output = [normalize_item(item) for item in plain(response).get('output', [])]
        return response
    def delta(self, *args, **kwargs):
        event = original_delta(self, *args, **kwargs)
        if getattr(event, 'type', None) == 'response.reasoning_summary_text.delta':
            # Preserve incremental timing without claiming raw Kimi thoughts
            # are provider-authored summaries. The opaque state item is done later.
            return None
        return event
    Iterator._transform_chat_completion_chunk_to_response_api_chunk = delta
    Iterator._emit_terminal_response_event = terminal
    Iterator.create_reasoning_output_item_done_event = reasoning_done
    Transform.transform_chat_completion_response_to_responses_api_response = staticmethod(transform)
    integration = common_request_processing.route_request
    def uninstall():
        Iterator._emit_terminal_response_event = original_terminal
        Iterator.create_reasoning_output_item_done_event = original_reasoning
        Iterator._transform_chat_completion_chunk_to_response_api_chunk = original_delta
        Transform.transform_chat_completion_response_to_responses_api_response = original_transform
    integration.uninstall = uninstall
    return integration
