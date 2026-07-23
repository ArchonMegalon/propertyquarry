from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent
BROKER = ROOT / "tools" / "verify-github-credential-stream.py"
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_github_credential_stream",
    BROKER,
)
assert SPEC is not None and SPEC.loader is not None
broker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = broker
SPEC.loader.exec_module(broker)


class CredentialStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        if (os.getuid(), os.getgid()) != (
            broker.CALLER_UID,
            broker.CALLER_GID,
        ):
            self.skipTest("credential stream contract requires uid/gid 1000")
        self.token = bytearray(b"github_pat_" + b"a" * 48)

    @staticmethod
    def _response(path: str) -> tuple[object, dict[str, str]]:
        metadata = {"x-accepted-github-permissions": "metadata=read"}
        if path == f"/repos/{broker.REPOSITORY}":
            return {
                "full_name": broker.REPOSITORY,
                "id": broker.REPOSITORY_ID,
                "owner": {"id": broker.REPOSITORY_OWNER_ID},
            }, metadata
        if path.startswith("/user/repos?"):
            return [
                {
                    "full_name": broker.REPOSITORY,
                    "id": broker.REPOSITORY_ID,
                    "owner": {"id": broker.REPOSITORY_OWNER_ID},
                }
            ], metadata
        if path.endswith("/actions/runners?per_page=1"):
            return {
                "runners": [],
                "total_count": 0,
            }, {"x-accepted-github-permissions": "administration=read"}
        if path.endswith("/actions/oidc/customization/sub"):
            return {
                "sub_claim_prefix": f"repo:{broker.REPOSITORY}",
                "use_default": True,
                "use_immutable_subject": True,
            }, {"x-accepted-github-permissions": "actions=read"}
        raise AssertionError(path)

    def test_exact_fine_grained_instance_reaches_every_probe(self) -> None:
        identities: list[int] = []
        paths: list[str] = []

        def request(
            token: bytearray, path: str
        ) -> tuple[object, dict[str, str]]:
            identities.append(id(token))
            paths.append(path)
            return self._response(path)

        broker.verify_github_credential(self.token, request)
        self.assertEqual(identities, [id(self.token)] * 4)
        self.assertEqual(
            paths,
            [
                f"/repos/{broker.REPOSITORY}",
                (
                    "/user/repos?affiliation="
                    "owner%2Ccollaborator%2Corganization_member&per_page=100"
                ),
                f"/repos/{broker.REPOSITORY}/actions/runners?per_page=1",
                (
                    f"/repos/{broker.REPOSITORY}/actions/"
                    "oidc/customization/sub"
                ),
            ],
        )

    def test_verified_instance_is_the_instance_forwarded_and_bound(self) -> None:
        identities: list[int] = []
        read_fd, write_fd = os.pipe()
        try:
            def verifier(token: bytearray) -> None:
                identities.append(id(token))

            def writer(path: str, token: bytearray) -> None:
                self.assertEqual(path, "/private/token.pipe")
                identities.append(id(token))

            expected = "sha256:" + hashlib.sha256(self.token).hexdigest()
            result = broker.verify_and_publish(
                self.token,
                write_fd,
                "/private/token.pipe",
                verifier=verifier,
                writer=writer,
            )
            os.close(write_fd)
            write_fd = -1
            status = os.read(read_fd, 256)
            self.assertEqual(result, expected)
            self.assertEqual(
                status,
                f"credential-instance-sha256={expected}\n".encode("ascii"),
            )
            self.assertEqual(identities, [id(self.token), id(self.token)])
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_caller_stream_accepts_only_one_fine_grained_token(self) -> None:
        for raw in (
            bytes(self.token),
            bytes(self.token) + b"\n",
        ):
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, raw)
                os.close(write_fd)
                write_fd = -1
                admitted = broker.read_token_fd(read_fd, timeout_seconds=0.5)
                self.assertEqual(admitted, self.token)
            finally:
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)

    def test_classic_broad_and_ambiguous_streams_fail_closed(self) -> None:
        for raw in (
            b"ghp_" + b"a" * 48,
            b"gho_" + b"a" * 48,
            b"ghu_" + b"a" * 48,
            b"ghs_" + b"a" * 48,
            b"ghr_" + b"a" * 48,
            bytes(self.token) + b"\n\n",
            bytes(self.token) + b" ",
        ):
            with self.subTest(prefix=raw[:4]):
                read_fd, write_fd = os.pipe()
                try:
                    os.write(write_fd, raw)
                    os.close(write_fd)
                    write_fd = -1
                    with self.assertRaises(broker.CredentialRejected):
                        broker.read_token_fd(read_fd, timeout_seconds=0.5)
                finally:
                    os.close(read_fd)
                    if write_fd >= 0:
                        os.close(write_fd)

    def test_multi_repository_and_oauth_scope_visibility_fail_closed(self) -> None:
        def multiple_repositories(
            token: bytearray, path: str
        ) -> tuple[object, dict[str, str]]:
            payload, headers = self._response(path)
            if path.startswith("/user/repos?"):
                assert isinstance(payload, list)
                payload.append(
                    {
                        "full_name": "ArchonMegalon/other",
                        "id": 2,
                        "owner": {"id": broker.REPOSITORY_OWNER_ID},
                    }
                )
            return payload, headers

        with self.assertRaises(broker.CredentialRejected):
            broker.verify_github_credential(
                self.token,
                multiple_repositories,
            )

        def classic_scope_header(
            token: bytearray, path: str
        ) -> tuple[object, dict[str, str]]:
            payload, headers = self._response(path)
            headers = dict(headers)
            headers["x-oauth-scopes"] = "repo, workflow"
            return payload, headers

        with self.assertRaises(broker.CredentialRejected):
            broker.verify_github_credential(
                self.token,
                classic_scope_header,
            )

    def test_surplus_or_missing_endpoint_permission_contract_fails_closed(
        self,
    ) -> None:
        def surplus_permission(
            token: bytearray, path: str
        ) -> tuple[object, dict[str, str]]:
            payload, headers = self._response(path)
            headers = dict(headers)
            if path.endswith("/actions/runners?per_page=1"):
                headers["x-accepted-github-permissions"] = (
                    "administration=read,contents=write"
                )
            return payload, headers

        with self.assertRaises(broker.CredentialRejected):
            broker.verify_github_credential(self.token, surplus_permission)

        def missing_permission(
            token: bytearray, path: str
        ) -> tuple[object, dict[str, str]]:
            payload, headers = self._response(path)
            if path.endswith("/actions/oidc/customization/sub"):
                return payload, {}
            return payload, headers

        with self.assertRaises(broker.CredentialRejected):
            broker.verify_github_credential(self.token, missing_permission)

    def test_duplicate_json_members_are_rejected(self) -> None:
        with self.assertRaises(broker.CredentialRejected):
            broker.strict_json(b'{"id":1,"id":2}')


if __name__ == "__main__":
    unittest.main()
