"""API tests for the org-scoped Metrics Map endpoints (/api/metrics-maps/)."""
import json

from django.test import TestCase, Client
from catalog.models import CustomUser, Organization, OrganizationMembership, MetricsMap


class MetricsMapApiTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.org = Organization.objects.create(name="Org A")
        self.user = CustomUser.objects.create_user(
            username="alice", email="alice@example.com", password="pw",
        )
        OrganizationMembership.objects.create(user=self.user, organization=self.org)

        # A second tenant, used to prove maps don't leak across orgs.
        self.other_org = Organization.objects.create(name="Org B")
        self.other_user = CustomUser.objects.create_user(
            username="bob", email="bob@example.com", password="pw",
        )
        OrganizationMembership.objects.create(user=self.other_user, organization=self.other_org)

    def _login(self, email="alice@example.com"):
        self.assertTrue(self.client.login(username=email, password="pw"))

    def test_requires_authentication(self):
        self.assertIn(self.client.get("/api/metrics-maps/").status_code, (401, 403))

    def test_create_stamps_org_and_user(self):
        self._login()
        payload = {
            "name": "Sales KPIs",
            "description": "Core revenue metrics",
            "metrics": [
                {"name": "Total Sales", "table": "Sales", "type": "measure",
                 "expression": "SUM(Sales[Amount])"},
            ],
        }
        res = self.client.post(
            "/api/metrics-maps/", data=json.dumps(payload), content_type="application/json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body["name"], "Sales KPIs")
        self.assertEqual(body["metric_count"], 1)

        obj = MetricsMap.objects.get(id=body["id"])
        # org + created_by are set server-side, not from the payload.
        self.assertEqual(obj.organization_id, self.org.id)
        self.assertEqual(obj.created_by_id, self.user.id)

    def test_create_ignores_spoofed_organization(self):
        self._login()
        res = self.client.post(
            "/api/metrics-maps/",
            data=json.dumps({"name": "X", "metrics": [], "organization": self.other_org.id}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        obj = MetricsMap.objects.get(id=res.json()["id"])
        self.assertEqual(obj.organization_id, self.org.id)  # not Org B

    def test_list_is_scoped_to_org(self):
        mine = MetricsMap.objects.create(name="Mine", organization=self.org)
        MetricsMap.objects.create(name="Theirs", organization=self.other_org)

        self._login()
        res = self.client.get("/api/metrics-maps/")
        self.assertEqual(res.status_code, 200)
        results = res.json()["results"]
        ids = {row["id"] for row in results}
        self.assertEqual(ids, {mine.id})

    def test_cannot_fetch_other_orgs_map(self):
        theirs = MetricsMap.objects.create(name="Theirs", organization=self.other_org)
        self._login()
        self.assertEqual(self.client.get(f"/api/metrics-maps/{theirs.id}/").status_code, 404)

    def test_update_metrics(self):
        m = MetricsMap.objects.create(name="Draft", organization=self.org, metrics=[])
        self._login()
        res = self.client.patch(
            f"/api/metrics-maps/{m.id}/",
            data=json.dumps({"metrics": [{"name": "Orders", "type": "measure"}]}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        m.refresh_from_db()
        self.assertEqual(len(m.metrics), 1)
        self.assertEqual(m.metrics[0]["name"], "Orders")
        self.assertEqual(m.organization_id, self.org.id)  # stays pinned

    def test_rejects_non_list_metrics(self):
        self._login()
        res = self.client.post(
            "/api/metrics-maps/",
            data=json.dumps({"name": "Bad", "metrics": {"not": "a list"}}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    # ---- public sharing ---------------------------------------------------

    GRAPH = {"meta": {"name": "M"}, "nodes": [{"id": "n1"}], "edges": [], "groups": []}

    def _share(self, map_id, **body):
        return self.client.post(
            f"/api/metrics-maps/{map_id}/share/",
            data=json.dumps(body), content_type="application/json",
        )

    def test_share_mints_token_and_defaults(self):
        m = MetricsMap.objects.create(name="Shared", organization=self.org,
                                      kind="canvas", graph=self.GRAPH)
        self._login()
        res = self._share(m.id)
        self.assertEqual(res.status_code, 200, res.content)
        token = res.json()["public_token"]
        self.assertTrue(token)
        self.assertIs(res.json()["public_can_drag"], True)  # model default
        m.refresh_from_db()
        self.assertEqual(str(m.public_token), token)

    def test_share_can_toggle_drag_without_rotating_token(self):
        m = MetricsMap.objects.create(name="Shared", organization=self.org,
                                      kind="canvas", graph=self.GRAPH)
        self._login()
        first = self._share(m.id).json()["public_token"]
        res = self._share(m.id, can_drag=False)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["public_token"], first)  # unchanged
        self.assertIs(res.json()["public_can_drag"], False)

    def test_share_rotate_changes_token(self):
        m = MetricsMap.objects.create(name="Shared", organization=self.org,
                                      kind="canvas", graph=self.GRAPH)
        self._login()
        first = self._share(m.id).json()["public_token"]
        second = self._share(m.id, rotate=True).json()["public_token"]
        self.assertNotEqual(first, second)

    def test_public_endpoint_is_anonymous_and_narrow(self):
        m = MetricsMap.objects.create(name="Shared", description="d",
                                      organization=self.org, kind="canvas",
                                      graph=self.GRAPH)
        self._login()
        token = self._share(m.id).json()["public_token"]
        self.client.logout()  # prove no auth is required

        res = self.client.get(f"/api/metrics-maps/public/{token}/")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["name"], "Shared")
        self.assertEqual(body["graph"], self.GRAPH)
        self.assertIn("public_can_drag", body)
        # Tenant metadata must never leak through the public projection.
        for leaked in ("organization", "created_by", "created_by_email", "public_token", "id"):
            self.assertNotIn(leaked, body)

    def test_unshare_kills_the_public_link(self):
        m = MetricsMap.objects.create(name="Shared", organization=self.org,
                                      kind="canvas", graph=self.GRAPH)
        self._login()
        token = self._share(m.id).json()["public_token"]

        res = self.client.delete(f"/api/metrics-maps/{m.id}/share/")
        self.assertEqual(res.status_code, 204)
        m.refresh_from_db()
        self.assertIsNone(m.public_token)
        # The old link now reads as "no longer available".
        self.assertEqual(self.client.get(f"/api/metrics-maps/public/{token}/").status_code, 404)

    def test_public_endpoint_404s_for_unknown_token(self):
        # A well-formed but unknown uuid is a flat 404 (no enumeration signal).
        res = self.client.get("/api/metrics-maps/public/00000000-0000-4000-8000-000000000000/")
        self.assertEqual(res.status_code, 404)

    def test_cannot_share_another_orgs_map(self):
        theirs = MetricsMap.objects.create(name="Theirs", organization=self.other_org,
                                           kind="canvas", graph=self.GRAPH)
        self._login()  # alice, Org A
        self.assertEqual(self._share(theirs.id).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.public_token)

    def test_public_token_cannot_be_set_via_plain_write(self):
        m = MetricsMap.objects.create(name="Mine", organization=self.org,
                                      kind="canvas", graph=self.GRAPH)
        self._login()
        spoof = "11111111-1111-4111-8111-111111111111"
        res = self.client.patch(
            f"/api/metrics-maps/{m.id}/",
            data=json.dumps({"public_token": spoof}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        m.refresh_from_db()
        self.assertIsNone(m.public_token)  # read-only — ignored
