#!/usr/bin/env python3
"""Diagnose observed consultation state. Does not operate or authorize a browser."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit


def display_text(value: str) -> str:
    # Browser rich-text rendering can change line endings and terminal newlines.
    # Preserve internal spaces, case, punctuation, and all substantive content.
    return value.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')


def assess(data: dict) -> dict:
    def result(action: str, reason: str, **extra) -> dict:
        return {'action': action, 'reason': reason, **extra}

    request = data['request']
    page = data['observation']
    phase = data['phase']
    if phase not in ('before_send', 'after_attempt', 'completion'):
        return result('inspect', 'unknown_phase')
    if not request['browser_id'] or not request['tab_id']:
        return result('inspect', 'missing_tab_identity')
    if (request['browser_id'], request['tab_id']) != (page['browser_id'], page['tab_id']):
        return result('inspect', 'wrong_browser_or_tab')
    url = urlsplit(page['url'])
    if url.scheme != 'https' or url.netloc != 'chatgpt.com':
        return result('inspect', 'wrong_origin')
    if url.path.startswith('/auth'):
        return result('inspect', 'login_required')
    expected_url = request.get('conversation_url')
    if expected_url and page['url'].split('#', 1)[0] != expected_url.split('#', 1)[0]:
        return result('inspect', 'conversation_changed')
    if page['capture_complete'] is not True:
        return result('inspect', 'incomplete_observation')
    if page.get('error_text'):
        return result('inspect', 'visible_page_error')

    users = page['user_turns']
    assistants = page['assistant_turns']
    user_ids = [turn['id'] for turn in users]
    assistant_ids = [turn['id'] for turn in assistants]
    if any(not ident for ident in user_ids + assistant_ids):
        return result('inspect', 'missing_turn_identity')
    if len(set(user_ids)) != len(user_ids) or len(set(assistant_ids)) != len(assistant_ids):
        return result('inspect', 'duplicate_turn_identity')
    baseline_users = request['baseline_user_ids']
    baseline_assistants = request['baseline_assistant_ids']
    wanted_text = display_text(request['prompt'])
    attachments = sorted(request['attachment_names'])
    new_users = [turn for turn in users if turn['id'] not in baseline_users]

    if phase == 'before_send':
        if request['send_attempted'] is not False:
            return result('inspect', 'send_already_attempted')
        if user_ids != baseline_users or assistant_ids != baseline_assistants:
            return result('inspect', 'conversation_changed_since_prepare')
        if page['generating'] is not False:
            return result('inspect', 'generation_active_or_unknown')
        # The caller discovers and verifies the target/maximum in the live UI first.
        # Matching raw labels detects drift; it is not proof that a model was identified well.
        if page['model_label'] != request['model_label']:
            return result('inspect', 'model_selection_changed')
        if page['composer_count'] != 1 or page['send_count'] != 1:
            return result('inspect', 'ambiguous_composer_or_send_control')
        if not wanted_text and not attachments:
            return result('inspect', 'empty_request')
        if display_text(page['composer_text']) != wanted_text:
            return result('inspect', 'composer_text_mismatch')
        if sorted(page['attachment_names']) != attachments:
            return result('inspect', 'attachment_mismatch')
        if page['attachments_ready'] is not True or page['send_enabled'] is not True:
            return result('inspect', 'request_not_ready')
        return result('ready_to_send', 'observed_request_matches')

    # Never recommend another send after an attempted submission. A visible draft can
    # remain while the server has accepted the request and the UI has not caught up.
    if request['send_attempted'] is not True:
        return result('inspect', 'attempt_record_missing')
    if not new_users:
        return result('inspect', 'submission_unknown_do_not_resend')
    if len(new_users) != 1 or users[-1]['id'] != new_users[0]['id']:
        return result('inspect', 'unexpected_new_user_turns')
    user = new_users[0]
    if display_text(user['text']) != wanted_text or sorted(user['attachment_names']) != attachments:
        return result('inspect', 'submitted_request_mismatch')
    if phase == 'after_attempt':
        return result('submitted', 'matching_new_user_turn', user_id=user['id'])

    candidates = [turn for turn in assistants
                  if turn['id'] not in baseline_assistants and turn['after_user_id'] == user['id']]
    if page['generating'] is True:
        return result('wait', 'generation_active', user_id=user['id'])
    if page['generating'] is not False:
        return result('inspect', 'generation_state_unknown')
    if not candidates:
        return result('wait', 'no_answer_for_this_request', user_id=user['id'])
    if len(candidates) != 1:
        return result('inspect', 'multiple_answer_candidates')
    answer = candidates[0]
    if answer.get('model_slug') and request.get('response_model_slug'):
        if answer['model_slug'] != request['response_model_slug']:
            return result('inspect', 'answer_model_mismatch')
    if not answer['text'].strip() or answer['final_actions'] is not True:
        return result('wait', 'answer_not_final')
    previous = data.get('previous_answer')
    if not previous or (previous['id'], previous['text']) != (answer['id'], answer['text']):
        return result('wait', 'confirm_answer_stability', assistant_id=answer['id'])
    return result('capture', 'new_answer_complete_and_stable',
                  user_id=user['id'], assistant_id=answer['id'])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state', type=Path, help='UTF-8 JSON with request and observed page state')
    args = parser.parse_args()
    try:
        data = json.loads(args.state.read_text(encoding='utf-8-sig'))
        outcome = assess(data)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        # Do not echo request text, file contents, or sensitive observations in diagnostics.
        outcome = {'action': 'inspect', 'reason': 'invalid_or_missing_observation_fields'}
    print(json.dumps(outcome, ensure_ascii=False))
    return 0 if outcome['action'] != 'inspect' else 2


if __name__ == '__main__':
    raise SystemExit(main())
