import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

# The host AstrBot runtime is not needed; all outbound messages are mocked.
for name in ('astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star', 'astrbot.api.message_components'):
    sys.modules[name] = types.ModuleType(name)
sys.modules['astrbot.api'].AstrBotConfig = dict
sys.modules['astrbot.api'].logger = MagicMock()
sys.modules['astrbot.api.event'].MessageChain = lambda **kw: kw
class Star:
    def __init__(self, context): self.context = context
sys.modules['astrbot.api.star'].Star = Star
sys.modules['astrbot.api.star'].Context = object
sys.modules['astrbot.api.star'].register = lambda *args: lambda cls: cls
sys.modules['astrbot.api.message_components'].Plain = lambda text: text
sys.modules['astrbot.api.message_components'].Image = types.SimpleNamespace(fromURL=lambda url: url)
spec = importlib.util.spec_from_file_location('monitor_under_test', Path(__file__).parents[1] / 'main.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

RAW = dict(tracker_enabled=True, tracker_api_url='http://tracker.example',
           tracker_watch_url='http://v.738888.xyz/#synctv', tracker_media_name='木柱的直播', push_target='test:group:1')

class ConfigTests(unittest.TestCase):
    def test_tracker_configuration_does_not_require_acfun(self):
        c = m._read_tracker_config(RAW)
        self.assertEqual(c.api_url, 'http://tracker.example')
        self.assertEqual(c.watch_url, 'http://v.738888.xyz/#synctv')
        self.assertIsNone(m._read_tracker_config({}))
        for change in ({'tracker_api_url': 'http://tracker/#synctv'}, {'tracker_api_url': 'http://u:p@host'}, {'tracker_media_id': '../x'}, {'push_target': ''}):
            with self.assertRaises(ValueError): m._read_tracker_config({**RAW, **change})

    def test_strict_status_validation(self):
        c = m._read_tracker_config(RAW)
        self.assertEqual(m._tracker_snapshot({'mediaId': 'med_3', 'active': True, 'startedAt': '123'}, c, 'med_3').live_id, 'med_3:123')
        for data in ({}, {'mediaId':'med_other','active':False}, {'mediaId':'med_3','active':'false'}, {'mediaId':'med_3','active':True,'startedAt':''}):
            with self.assertRaises(ValueError): m._tracker_snapshot(data, c, 'med_3')

    def test_acfun_parser_and_config_remain_available(self):
        c = m._read_config({'room_url':'https://live.acfun.cn/live/123','push_target':'target'})
        self.assertEqual(c.push_target, 'target')
        html = 'window.__INITIAL_STATE__ = ' + json.dumps({'liveInfo': {'result':0,'liveId':'abc','title':'title','user':{'name':'user'}}})
        self.assertEqual(m._extract_snapshot(html).live_id, 'abc')
        self.assertFalse(m._extract_snapshot('window.__INITIAL_STATE__ = {"liveInfo":{"result":0}}').is_live)

class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = m.AcFunLiveMonitor(object(), RAW)
        self.plugin._send_notification = AsyncMock(return_value=True)
        self.config = m._read_tracker_config(RAW)

    async def test_baseline_transitions_new_sessions_and_dedup(self):
        off = m.LiveSnapshot(False)
        first = m.LiveSnapshot(True, 'med_3:1')
        second = m.LiveSnapshot(True, 'med_3:2')
        for snapshot in (off, off, first, first, second, off, off):
            await self.plugin._handle_tracker_snapshot(self.config, snapshot)
        calls = self.plugin._send_notification.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0].args[1].startswith('🟢 木柱的直播'))
        self.assertTrue(calls[-1].args[1].startswith('🔴 木柱的直播'))
        for call in calls:
            self.assertEqual(call.args[0], RAW['push_target'])
            self.assertIn(RAW['tracker_watch_url'], call.args[1])
            self.assertEqual(call.args[2], '')
        self.assertFalse(self.plugin._initialized)  # independent AcFun state

    async def test_startup_during_live_is_silent(self):
        await self.plugin._handle_tracker_snapshot(self.config, m.LiveSnapshot(True, 'med_3:1'))
        self.plugin._send_notification.assert_not_called()

    async def test_send_failure_retries_without_committing_state(self):
        off, live = m.LiveSnapshot(False), m.LiveSnapshot(True, 'med_3:1')
        await self.plugin._handle_tracker_snapshot(self.config, off)
        self.plugin._send_notification.side_effect = [False, RuntimeError('offline bot'), True]
        for _ in range(2):
            with self.assertRaises(RuntimeError): await self.plugin._handle_tracker_snapshot(self.config, live)
            self.assertIs(self.plugin._tracker_previous, off)
        await self.plugin._handle_tracker_snapshot(self.config, live)
        self.assertIs(self.plugin._tracker_previous, live)

    async def test_lookup_pagination_and_explicit_id(self):
        self.plugin._tracker_get = AsyncMock(side_effect=[
            {'media':[{'name':'other','id':f'med_{i}'} for i in range(50)]},
            {'media':[{'name':'木柱的直播','id':'med_3','sourceProvider':5}]},
            {'mediaId':'med_3','active':True,'startedAt':'123'},
        ])
        snap = await self.plugin._fetch_tracker_snapshot(self.config)
        self.assertEqual(snap.live_id, 'med_3:123')
        self.assertEqual(self.plugin._tracker_get.call_args_list[1].args[2]['page'], 2)
        self.plugin._tracker_get.reset_mock(side_effect=True)
        self.plugin._tracker_get.return_value = {'mediaId':'med_3','active':False,'startedAt':''}
        explicit = m._read_tracker_config({**RAW, 'tracker_media_id':'med_3'})
        self.assertFalse((await self.plugin._fetch_tracker_snapshot(explicit)).is_live)
        self.assertEqual(self.plugin._tracker_get.call_args.args[1], 'live-status')

    async def test_missing_ambiguous_and_wrong_provider_do_not_report_offline(self):
        live = m.LiveSnapshot(True, 'med_3:1')
        self.plugin._tracker_previous = live
        for items in ([], [{'name':'木柱的直播','id':f'med_{i}','sourceProvider':5} for i in range(2)], [{'name':'木柱的直播','id':'med_3','sourceProvider':2}]):
            self.plugin._tracker_get = AsyncMock(return_value={'media':items})
            with self.assertRaises(ValueError): await self.plugin._fetch_tracker_snapshot(self.config)
            self.assertIs(self.plugin._tracker_previous, live)
        self.plugin._send_notification.assert_not_called()

    async def test_http_boundary_and_unavailable_endpoint(self):
        seen = []
        async def status(request):
            seen.append((request.path, dict(request.query), request.headers.get('Authorization')))
            if request.query['mediaId']=='med_4': return web.Response(status=503)
            return web.json_response({'mediaId':'med_3','active':False,'startedAt':''})
        app = web.Application(); app.router.add_get('/api/synctv/live-status', status)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.plugin._session = m.aiohttp.ClientSession()
        try:
            config = m._read_tracker_config({**RAW,'tracker_api_url':f'http://127.0.0.1:{port}','tracker_media_id':'med_3'})
            self.assertFalse((await self.plugin._fetch_tracker_snapshot(config)).is_live)
            with self.assertRaises(m.aiohttp.ClientResponseError):
                await self.plugin._tracker_get(config, 'live-status', {'mediaId':'med_4'})
            self.assertEqual(seen[0], ('/api/synctv/live-status', {'mediaId':'med_3'}, None))
        finally:
            await self.plugin._session.close(); await runner.cleanup()

    async def test_both_poll_tasks_are_cancelled_before_session_close(self):
        started = []
        async def wait(label):
            started.append(label)
            await asyncio.Event().wait()
        self.plugin._poll_loop = lambda: wait('acfun')
        self.plugin._tracker_poll_loop = lambda: wait('tracker')
        await self.plugin.initialize()
        await asyncio.sleep(0)
        tasks = (self.plugin._task, self.plugin._tracker_task)
        await self.plugin.terminate()
        self.assertEqual(started, ['acfun','tracker'])
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertTrue(self.plugin._session.closed)

if __name__ == '__main__': unittest.main()
