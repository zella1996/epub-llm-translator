import json

import pytest

from translator.llm_api import LLMError, OpenAICompatibleClient, OpenAIConfig
from translator.reading_eval import prepare_reading_input
from translator.translator import generate_reading_assistance
from translator.trace import ParagraphTrace


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_invalid_json_uses_one_explicit_repair_not_transport_retries(repair_succeeds, tmp_path):
    malformed = '{"text": "say "hello" politely"}'
    valid = json.dumps({"sentences": [{"index": 1, "translation": "他离开了。",
                                      "difficulty": "fluent"}], "aids": []})
    requests = []

    def transport(endpoint, payload, headers, timeout):
        requests.append(payload)
        content = valid if repair_succeeds and len(requests) == 2 else malformed
        return {"choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}

    model = OpenAICompatibleClient(OpenAIConfig(base_url="https://example.invalid/v1",
        model="fake", max_retries=2), transport=transport)
    prepared = prepare_reading_input({"paragraph": "He left."})
    trace = ParagraphTrace(tmp_path / 'trace')
    if repair_succeeds:
        result = generate_reading_assistance(prepared, model, pipeline="single",
                                             trace=trace, repair_core=True)
        assert result.status == "complete"
    else:
        with pytest.raises(LLMError):
            generate_reading_assistance(prepared, model, pipeline="single",
                                        trace=trace, repair_core=True)
    assert len(requests) == 2
    assert json.loads(requests[1]['messages'][1]['content'])['invalid_draft'] == malformed
    assert model.usage.total_tokens == 10
    original = json.loads((trace.directory / 'reading-single-attempt-01-response.json').read_text())
    assert original['status'] == 'failure'
    assert original['provider_responses'][0]['choices'][0]['message']['content'] == malformed
    assert (trace.directory / 'reading-core-repair-attempt-01-response.json').exists()


@pytest.mark.parametrize('finish_reason,enabled', [('length', True), ('stop', False)])
def test_no_repair_for_truncated_output_or_disabled_policy(finish_reason, enabled):
    calls = []
    def transport(*args):
        calls.append(args)
        return {'choices': [{'finish_reason': finish_reason,
                             'message': {'content': '{"text": "unfinished'}}]}
    model = OpenAICompatibleClient(OpenAIConfig(base_url='https://example.invalid/v1',
        model='fake', max_retries=0), transport=transport)
    with pytest.raises(LLMError):
        generate_reading_assistance(prepare_reading_input({'paragraph': 'He left.'}),
            model, pipeline='single', trace=None, repair_core=enabled)
    assert len(calls) == 1


def test_repair_does_not_bypass_sentence_coverage_validation():
    from translator.reading_assistance import AssistanceError
    contents = iter(['{"text": "say "hello""}', '{"sentences": [], "aids": []}'])
    def transport(*args):
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': next(contents)}}]}
    model = OpenAICompatibleClient(OpenAIConfig(base_url='https://example.invalid/v1',
        model='fake', max_retries=0), transport=transport)
    with pytest.raises(AssistanceError):
        generate_reading_assistance(prepare_reading_input({'paragraph': 'He left.'}),
            model, pipeline='single', trace=None, repair_core=True)
