"""Invalid app-search batches are rejected before any physical operation."""

import asyncio

import pytest
from test_controls import FakePhone, Screen, instant_mirror  # noqa: F401

from hidagent.app import ControlAction
from hidagent.control import Controls


class SearchPhone(FakePhone):
    app_search_available = True

    async def open_app_search(self, text, **kwargs):
        self.calls.append(('search', text))
        return (.1, .1, .9, .2)


@pytest.mark.parametrize('query', ['', '设置', ' Freeform', 'Freeform ', 'x' * 65, 'a\nb'])
def test_invalid_search_in_batch_never_executes_preceding_point(query):
    phone = SearchPhone()
    controls = Controls(phone, Screen())
    observation = controls.observation()
    requests = [ControlAction(action='point', screen_id=observation['screen_id'], x=.5, y=.5),
                ControlAction(action='open_app', screen_id=observation['screen_id'], text=query)]
    with pytest.raises(ValueError, match='ASCII'):
        asyncio.run(controls.perform_batch(observation['screen_id'], requests))
    assert phone.calls == []
