#!/usr/bin/env python
import os
from datetime import datetime

import click
from flask import Flask, g, redirect, render_template, request, send_file, url_for
from flask_cdn import CDN
from flask_login import current_user as current_login_user
from flask_login import login_required, logout_user
from flask_recaptcha import ReCaptcha
from social_flask.template_filters import backends
from social_flask.utils import load_strategy
from sqlalchemy.sql import func

from member_card import utils
from member_card.db import squarespace_orders_etl
from member_card.models import User
from member_card.squarespace import Squarespace

BASE_DIR = os.path.dirname(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "member_card")
)


app = Flask(__name__)
logger = app.logger
logger.propagate = False

login_manager = utils.MembershipLoginManager()

recaptcha = ReCaptcha()

cdn = CDN()


@login_manager.user_loader
def load_user(userid):
    try:
        return User.query.get(int(userid))
    except (TypeError, ValueError):
        pass


@app.before_request
def global_user():
    # evaluate proxy value
    g.user = current_login_user._get_current_object()


@app.teardown_appcontext
def commit_on_success(error=None):
    if "sqlalchemy" not in app.extensions:
        # TODO: do this better
        return
    from member_card.db import db

    if error is None:
        db.session.commit()
    else:
        db.session.rollback()

    db.session.remove()


@app.context_processor
def inject_user():
    try:
        return {"user": g.user}
    except AttributeError:
        return {"user": None}


@app.context_processor
def load_common_context():
    from member_card.db import get_membership_table_last_sync

    return utils.common_context(
        app.config["SOCIAL_AUTH_AUTHENTICATION_BACKENDS"],
        load_strategy(),
        getattr(g, "user", None),
        app.config.get("SOCIAL_AUTH_GOOGLE_PLUS_KEY"),
        membership_last_sync=get_membership_table_last_sync(),
    )


app.context_processor(backends)
app.jinja_env.globals["url"] = utils.social_url_for


@app.route("/")
@login_required
def home():
    from member_card.models import AnnualMembership

    current_user = g.user
    if not current_user.is_authenticated:
        return redirect("/login")

    if current_user.has_active_memberships:
        from member_card.models.membership_card import get_or_create_membership_card

        membership_card = get_or_create_membership_card(current_user)
        # response_body = render_template(
        # TODO: update this deal to only generate gpay pass JWTs upon demand instead of every request
        return render_template(
            "member_card_and_history.html.j2",
            membership_card=membership_card,
            membership_orders=g.user.annual_memberships,
            membership_table_keys=list(AnnualMembership().to_dict().keys()),
        )
    else:
        return render_template(
            "no_membership_landing_page.html.j2",
            user=current_user,
        )


@app.route("/email-distribution-request", methods=["POST"])
def email_distribution_request():
    from email_validator import EmailNotValidError, validate_email

    from member_card.pubsub import publish_message

    log_extra = dict(form=request.form)

    # First prerequisite: verified recaptcha stuff:
    if not recaptcha.verify():
        email_form_error_message = "Request not verified via ReCaptcha! Please try again or contact support@losverd.es for assistance."
        logger.error(
            "Unable to verify recaptcha, redirecting to login", extra=log_extra
        )
        return redirect(
            f"{url_for('login')}?emailFormErrorMessage={email_form_error_message}"
        )

    email_distribution_recipient = request.form["emailDistributionRecipient"]
    log_extra.update(dict(email_distribution_recipient=email_distribution_recipient))

    # Second prerequisite: we can actually send to this address
    try:
        # Validate.
        valid = validate_email(email_distribution_recipient)

        # Update with the normalized form.
        email_distribution_recipient = valid.email
        log_extra.update(
            dict(email_distribution_recipient=email_distribution_recipient)
        )
    except EmailNotValidError as err:
        log_extra.update(dict(err=err))
        # email is not valid, exception message is human-readable
        email_form_error_message = str(err)
        logger.error(
            "Unable to validate email, redirecting to login",
            extra=dict(form=request.form),
        )
        return redirect(
            f"{url_for('login')}?emailFormErrorMessage={email_form_error_message}"
        )

    topic_id = app.config["GCLOUD_PUBSUB_TOPIC_ID"]
    logger.info(
        f"publishing email distribution request to pubsub {topic_id=}", extra=log_extra
    )
    publish_message(
        project_id=app.config["GCLOUD_PROJECT"],
        topic_id=topic_id,
        message_data=dict(
            type="email_distribution_request",
            email_distribution_recipient=email_distribution_recipient,
            remote_addr=request.remote_addr,
            submitted_on=datetime.utcnow().isoformat(),
        ),
    )

    return render_template(
        "email_request_landing_page.html.j2",
        submitted_email=email_distribution_recipient,
        submission_response_msg="Request received",
        redirect_home_delay_seconds="45",
    )


@login_required
@app.route("/passes/apple-pay")
def passes_apple_pay():

    current_user = g.user
    if current_user.is_authenticated:
        from member_card.passes import get_apple_pass_for_user

        attachment_filename = f"lv_apple_pass-{current_user.last_name.lower()}.pkpass"
        pkpass_out_path = get_apple_pass_for_user(
            user=current_user,
        )
        return send_file(
            pkpass_out_path,
            attachment_filename=attachment_filename,
            mimetype="application/vnd.apple.pkpass",
            as_attachment=True,
        )
    return redirect(url_for("home"))


@login_required
@app.route("/verify-pass/<serial_number>")
# Note: get_or_create_membership_card() has this route hard-coded in it
# TODO: ^ make that not the case
def verify_pass(serial_number):
    from member_card.db import db
    from member_card.models import AnnualMembership, MembershipCard

    signature = request.args.get("signature")
    if not signature:
        return "Unable to verify signature!", 401

    signature_verified = utils.verify(signature=signature, data=serial_number)
    if not signature_verified:
        return "Unable to verify signature!", 401

    verified_card = (
        db.session.query(MembershipCard).filter_by(serial_number=serial_number).one()
    )
    logger.debug(f"{verified_card=}")

    return render_template(
        "apple_pass_validation.html.j2",
        validating_user=g.user,
        verified_card=verified_card,
        membership_table_keys=list(AnnualMembership().to_dict().keys()),
    )


@app.route("/login")
def login():
    """Logout view"""
    email_form_error_message = request.args.get("emailFormErrorMessage", "")
    return render_template(
        "login.html.j2",
        email_form_error_message=email_form_error_message,
        recaptcha_site_key=app.config["RECAPTCHA_SITE_KEY"],
    )


@login_required
@app.route("/logout")
def logout():
    logout_user()
    return redirect("/")


@app.route("/privacy-policy")
def privacy_policy():
    return render_template(
        "privacy_policy.html.j2",
    )


@app.route("/about")
def about():
    return render_template(
        "about.html.j2",
    )


@app.cli.command("ensure-db-schemas")
@click.option("-D", "--drop-first", default=False)
def ensure_db_schemas(drop_first):
    logger.debug("ensure-db-schemas: calling `db.create_all()`")
    from member_card.db import ensure_db_schemas

    ensure_db_schemas(drop_first)


@app.cli.command("sync-subscriptions")
@click.option("-l", "--load-all", default=False)
def sync_subscriptions(load_all):
    from member_card.db import db

    membership_skus = app.config["SQUARESPACE_MEMBERSHIP_SKUS"]
    squarespace = Squarespace(api_key=app.config["SQUARESPACE_API_KEY"])
    etl_results = squarespace_orders_etl(
        squarespace_client=squarespace,
        db_session=db.session,
        membership_skus=membership_skus,
        load_all=load_all,
    )
    logger.info(f"sync_subscriptions() => {etl_results=}")


@app.cli.command("recreate-user")
@click.argument("email")
def recreate_user(email):
    from social_core.actions import do_disconnect
    from social_flask.utils import load_strategy

    from member_card.db import db, get_or_create
    from member_card.models import User
    from member_card.utils import associations

    user = User.query.filter_by(email=email).one()
    memberships = list(user.annual_memberships)
    user_associations = associations(user=user, strategy=load_strategy())
    for association in user_associations:
        with app.app_context():
            disconnect_resp = do_disconnect(
                backend=association.get_backend_instance(load_strategy()),
                user=user,
                association_id=association.id,
            )
            logger.info(f"{disconnect_resp=}")

    db.session.delete(user)
    db.session.commit()
    member_user = get_or_create(
        session=db.session,
        model=User,
        email=email,
    )
    member_user.memberships = memberships
    db.session.add(member_user)
    db.session.commit()
    logger.debug(f"{memberships=}")


@app.cli.command("update-sendgrid-template")
def update_sendgrid_template():
    from member_card.sendgrid import update_sendgrid_template

    update_sendgrid_template()


@app.cli.command("send-test-email")
@click.argument("email")
def send_test_email(email):
    from member_card.sendgrid import generate_and_send_email

    generate_and_send_email(
        user=User.query.filter_by(email=email).one(),
    )


@app.cli.command("generate-card-image")
@click.argument("email")
def generate_card_image(email):
    from member_card.image import generate_card_image
    from member_card.models.membership_card import get_or_create_membership_card

    user = User.query.filter_by(email=email).one()
    membership_card = get_or_create_membership_card(
        user=user,
    )
    output_path = app.config["BASE_DIR"]
    logger.info(f"Generating image of {membership_card=} for {user=} to {output_path=}")
    generate_card_image(
        membership_card=membership_card,
        output_path=output_path,
    )


@app.cli.command("query-db")
@click.argument("email")
def query_db(email):
    from member_card.models import AnnualMembership

    memberships = (
        AnnualMembership.query.filter_by(customer_email=func.lower(email))
        .order_by(AnnualMembership.created_on.desc())
        .all()
    )

    logger.info(f"memberships matching {email}:\n{memberships}")
    user = User.query.filter_by(email=func.lower(email)).one()
    logger.info(f"user matching {email}:\n{user}")
    logger.info(f"user memberships:\n{user.annual_memberships}")
    logger.info(f"user membership cards:\n{user.membership_cards}")


@app.cli.command("query-order-num")
@click.argument("order_num")
def query_order_num(order_num):
    from member_card.models import AnnualMembership

    memberships = (
        AnnualMembership.query.filter_by(order_number=order_num)
        .order_by(AnnualMembership.created_on.desc())
        .all()
    )

    logger.info(f"memberships matching {order_num}:\n{memberships}")
    users = [m.user for m in memberships]
    logger.info(f"user matching {order_num}:\n{users}")
    for user in users:
        logger.info(f"user memberships:\n{user.annual_memberships}")
        logger.info(f"user membership cards:\n{user.membership_cards}")


@app.cli.command("create-apple-pass")
@click.argument("email")
@click.option("-z", "--zip-file-path")
def create_apple_pass_cli(email, zip_file_path=None):
    create_apple_pass(email=email, zip_file=zip_file_path)


def create_apple_pass(email, zip_file=None):
    pass


@app.cli.command("force-assets-bundle-build")
def force_assets_bundle_build():
    utils.force_assets_bundle_build(app)


@app.cli.command("upload-statics")
def upload_statics():
    from member_card.cloudbuild import create_upload_statics_build

    create_upload_statics_build()


@app.cli.command("build-image")
@click.argument("image_name")
def build_image(image_name):
    from member_card.cloudbuild import create_docker_image_build

    build_result = create_docker_image_build(image_name=image_name)
    print(f"::set-output name=image::{build_result.images[0]}")


@app.cli.command("insert-google-pass-class")
def insert_google_pass_class():
    from member_card import gpay

    class_id = app.config["GOOGLE_PAY_PASS_CLASS_ID"]
    pass_class_payload = gpay.GooglePayPassClass(class_id).to_dict()

    insert_class_response = gpay.new_client().insert_class(
        class_id=class_id,
        payload=pass_class_payload,
    )
    logger.debug(f"Class ID: {class_id} insert response: {insert_class_response=}")


@app.cli.command("update-google-pass-class")
def update_google_pass_class():
    from member_card import gpay

    class_id = app.config["GOOGLE_PAY_PASS_CLASS_ID"]
    pass_class_payload = gpay.GooglePayPassClass(class_id).to_dict()

    update_class_response = gpay.new_client().patch_class(
        class_id=class_id,
        payload=pass_class_payload,
    )
    logger.debug(f"Class ID: {class_id} update response: {update_class_response=}")


@app.cli.command("demo-google-pay-pass")
@click.argument("email")
def demo_google_pay_pass(email):
    from member_card import gpay
    from member_card.models.membership_card import get_or_create_membership_card

    SAVE_LINK = "https://pay.google.com/gp/v/save/"

    user = User.query.filter_by(email=email).one()
    membership_card = get_or_create_membership_card(
        user=user,
    )

    pass_jwt = gpay.generate_pass_jwt(
        membership_card=membership_card,
    )

    print(f"This is an 'object' jwt:\n{pass_jwt.decode('UTF-8')}\n")
    print(
        "you can decode it with a tool to see the unsigned JWT representation:\nhttps://jwt.io\n"
    )
    print(f"Try this save link in your browser:\n{SAVE_LINK}{pass_jwt.decode('UTF-8')}")


@app.cli.command("apple-serial-num-to-hex")
@click.argument("serial_num")
def apple_serial_num_to_hex(serial_num):
    from uuid import UUID

    print(UUID(int=int(serial_num)))


@app.cli.command("publish-sync-subscriptions-msg")
def publish_sync_subscriptions_msg():

    from member_card.pubsub import publish_message

    topic_id = app.config["GCLOUD_PUBSUB_TOPIC_ID"]
    logger.info(f"publishing sync_subscriptions_etl message to pubsub {topic_id=}")
    publish_message(
        project_id=app.config["GCLOUD_PROJECT"],
        topic_id=topic_id,
        message_data=dict(
            type="sync_subscriptions_etl",
        ),
    )


@app.cli.command("add-memberships-to-user-email")
@click.argument("order_email")
@click.argument("user_email")
def add_memberships_to_user_email(order_email, user_email):
    logger.debug(f"{order_email=} => {user_email=}")
    from member_card.db import db
    from member_card.models import AnnualMembership

    memberships = (
        AnnualMembership.query.filter_by(customer_email=order_email)
        .order_by(AnnualMembership.created_on.desc())
        .all()
    )
    logger.debug(f"memberships matching {order_email}: {memberships}")

    user = User.query.filter_by(email=user_email).one()
    logger.debug(f"user returned for {user_email}: {user=}")
    logger.info(f"Adding memberships orders from {order_email} to: {user_email}")
    for membership in memberships:
        logger.debug(
            f"setting user_id attribute on {membership=} from {membership.user_id} to: {user.id}"
        )
        setattr(membership, "user_id", user.id)
        db.session.add(membership)
        db.session.commit()


@app.cli.command("update-user-name")
@click.argument("user_email")
@click.argument("first_name")
@click.argument("last_name")
def update_user_name(user_email, first_name, last_name):
    logger.debug(f"{user_email=} => {first_name=} {last_name=}")
    from member_card.db import db

    user = User.query.filter_by(email=func.lower(user_email)).one()
    logger.debug(f"user returned for {user_email}: {user=}")
    logger.info(
        f"Update name for {user} from {user.fullname} to: {first_name} {last_name}"
    )
    setattr(user, "fullname", " ".join([first_name, last_name]))
    setattr(user, "first_name", first_name)
    setattr(user, "last_name", last_name)
    db.session.add(user)
    db.session.commit()
    logger.debug(f"post-commit: {user=}")
