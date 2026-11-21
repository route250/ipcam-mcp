import unittest

from ipcam_mcp.app import LocalRequests


class HostAccessTest(unittest.IsolatedAsyncioTestCase):
    async def request(self, host, origin=None, kind='http'):
        called = []
        output = []

        async def downstream(scope, receive, send):
            called.append(True)

        async def send(message):
            output.append(message)

        headers = [(b'host', host.encode())]
        if origin is not None:
            headers.append((b'origin', origin.encode()))
        middleware = LocalRequests(downstream, {'pi4:9928', '192.168.0.105:9928'})
        await middleware({'type': kind, 'headers': headers}, None, send)
        return bool(called), output

    async def test_explicit_aliases_are_allowed(self):
        for host in ('pi4:9928', '192.168.0.105:9928'):
            self.assertTrue((await self.request(host))[0])
            self.assertTrue((await self.request(host, 'http://' + host, 'websocket'))[0])

    async def test_other_hosts_ports_and_origins_are_rejected(self):
        for host, origin in [('other:9928', None), ('pi4:8000', None),
                             ('pi4:9928', 'http://other:9928'),
                             ('pi4:9928', 'http://192.168.0.105:9928')]:
            called, output = await self.request(host, origin)
            self.assertFalse(called)
            self.assertEqual(output[0]['status'], 403)

    async def test_websocket_requires_origin(self):
        called, output = await self.request('pi4:9928', kind='websocket')
        self.assertFalse(called)
        self.assertEqual(output[0]['type'], 'websocket.close')
