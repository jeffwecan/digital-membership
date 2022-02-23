import base64
import json
import logging
from member_card import worker

logging.basicConfig(level=logging.DEBUG)


class TestPubsubIngress:
    @staticmethod
    def generate_test_envelope(message):
        test_envelope = dict(
            message=dict(
                data=base64.b64encode(json.dumps(message).encode("utf-8")).decode(
                    "utf-8"
                ),
            ),
        )
        return test_envelope

    def test_get_returns_method_not_allowed(self, client):
        response = client.get("/pubsub")
        logging.debug(f"{response=}")
        # Check that this method is indeed _not_ allowed
        assert response.status_code == 405

    def test_post_with_no_body(self, client):
        response = client.post("/pubsub")
        logging.debug(f"{response=}")

        # Check that we return a 400 / Bad Request in these cases
        assert response.status_code == 400

    def test_malformed_message_type(self, client):
        response = client.post(
            "/pubsub",
            json={"is hot garbage": True},
        )
        logging.debug(f"{response=}")

        # Check that we return a 400 / Bad Request in these cases
        assert response.status_code == 400

    def test_unsupported_message_type(self, client):
        test_message = dict(type="not-a-supported-type")
        response = client.post(
            "/pubsub",
            json=self.generate_test_envelope(test_message),
        )
        logging.debug(f"{response=}")

        # Check that we return a 400 / Bad Request in these cases
        assert response.status_code == 400

    def test_email_distribution_request(self, client):
        test_message = dict(
            type="email_distribution_request",
            email_distribution_recipient="los.verdes.tester+pls-no-matchy@gmail.com",
        )
        response = client.post(
            "/pubsub",
            json=self.generate_test_envelope(test_message),
        )
        logging.debug(f"{response=}")

        # Check that we return a 400 / Bad Request in these cases
        assert response.status_code == 204


class TestEmailDistribution:
    def test_no_matching_user(self, mocker):
        mock_send_email = mocker.patch("member_card.worker.generate_and_send_email")
        test_message = dict(
            type="email_distribution_request",
            email_distribution_recipient="los.verdes.tester+pls-no-matchy@gmail.com",
        )
        return_value = worker.process_email_distribution_request(
            message=test_message,
        )
        logging.debug(f"{return_value=}")
        assert return_value is None
        mock_send_email.assert_not_called()

    def test_with_matching_user_no_memberships(self, mocker, fake_user):
        mock_send_email = mocker.patch("member_card.worker.generate_and_send_email")
        test_message = dict(
            type="email_distribution_request",
            email_distribution_recipient=fake_user.email,
        )
        return_value = worker.process_email_distribution_request(
            message=test_message,
        )
        logging.debug(f"{return_value=}")
        assert return_value is None
        mock_send_email.assert_not_called()

    def test_with_matching_user_with_memberships(self, mocker, fake_member):
        mock_send_email = mocker.patch("member_card.worker.generate_and_send_email")
        test_message = dict(
            type="email_distribution_request",
            email_distribution_recipient=fake_member.email,
        )
        return_value = worker.process_email_distribution_request(
            message=test_message,
        )
        logging.debug(f"{return_value=}")
        assert return_value is mock_send_email.return_value
        mock_send_email.assert_called_once()
