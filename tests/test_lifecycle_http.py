"""Controller capability is independent of browser and telemetry authorization."""
import json
import os
import unittest

import test_dashboard


@unittest.skipIf(os.environ.get('ROUTING_SKIP_SOCKET_TESTS')=='1' and not os.environ.get('CI'),'Restricted sockets; mandatory HTTP in CI')
class LifecycleHTTP(unittest.TestCase):
    request=test_dashboard.DashboardHTTP.request
    authenticated=test_dashboard.DashboardHTTP.authenticated

    def skipTest(self,reason):
        if os.environ.get('CI'):self.fail(reason)
        super().skipTest(reason)

    def setUp(self):
        test_dashboard.DashboardHTTP.setUp(self)
        self.control='synthetic-control-capability-for-tests-only-1234567890'
        self.exporter='synthetic-exporter-capability-for-tests-only-123456789'
        self.server.control_capability=self.control
        self.server.otel_capability=self.exporter
        self.server.instance_id='11111111-1111-4111-8111-111111111111'
        self.server.revision='synthetic-reviewed-version'

    def control_request(self,action='status',**extra):
        return self.request('POST','/api/lifecycle',json.dumps({'action':action}),
            {'Content-Type':'application/json','X-Routing-Control':self.control,**extra})

    def test_independent_authorization_host_browser_and_body_bounds(self):
        self.assertEqual(self.authenticated('POST','/api/lifecycle',{'action':'status'})[0].status,403)
        for headers in ({'X-Routing-Control':self.exporter},{'Origin':self.server.origin},
                        {'Sec-Fetch-Site':'none'},{'Sec-Fetch-Mode':'cors'},{'Host':'localhost:'+str(self.server.server_port)}):
            self.assertEqual(self.control_request(**headers)[0].status,403)
        self.assertEqual(self.request('GET','/api/state',headers={'X-Routing-Control':self.control})[0].status,403)
        self.assertEqual(self.control_request(**{'Content-Encoding':'gzip'})[0].status,415)
        self.assertEqual(self.request('POST','/api/lifecycle','x'*513,
            {'Content-Type':'application/json','X-Routing-Control':self.control})[0].status,413)
        self.assertEqual(self.control_request('execute')[0].status,400)
        response,body=self.control_request()
        self.assertEqual(response.status,200)
        result=json.loads(body)
        self.assertEqual(result['instance_id'],self.server.instance_id)
        for secret in (self.control,self.exporter,self.csrf,self.server.bootstrap):self.assertNotIn(secret.encode(),body)
        self.assertNotIn('config',result)

    def test_reopen_rotates_single_use_handoff_without_invalidating_browser(self):
        old=self.server.bootstrap
        response,body=self.control_request('open');self.assertEqual(response.status,200)
        launch=test_dashboard.Path(json.loads(body)['launch_file'])
        self.assertTrue(launch.is_file());self.assertNotEqual(self.server.bootstrap,old)
        self.assertFalse(self.server.authorize(old))
        self.assertEqual(self.authenticated('GET','/api/state')[0].status,200)
        self.assertTrue(self.server.authorize(self.server.bootstrap));self.assertFalse(launch.exists())
        self.assertEqual(self.control_request('stop')[0].status,200)


if __name__=='__main__':unittest.main()
