import os
import json
import matcher
import hashlib
import boto3
from pathlib import Path
from slackeventsapi import SlackEventAdapter
from airtable import airtable
from flask import Flask, request, make_response, Response
from unittest import mock
import slack


application = Flask(__name__)
configuration = json.loads(
    boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ.get("SECRET_ARN")
        ).get("SecretString")
    )
slack_client = (
    slack.WebClient(token)
    if (token := configuration.get("slack_token")) else
    mock.MagicMock(slack.WebClient)
    )
slack_event_adapter = SlackEventAdapter(
    configuration.get("slack_signing_secret", "a"),
    "/events",
    application
    )
airtable_volunteers=airtable.Airtable(
    configuration["airtable_volunteers_base"],
    api_key=configuration["airtable_token"]
    )
airtable_mails=airtable.Airtable(
    configuration["airtable_mails_base"],
    api_key=configuration["airtable_token"]
    )
function_prefix = os.environ.get("FUNCTION_PREFIX")

data_directory = Path(__file__).parent / "data"

with open(data_directory / "elements" / "form.json", "r") as form_file:
    form = json.load(form_file)
with open(data_directory / "model.json", "r") as model_file:
    model = json.load(model_file)
with open(data_directory / "elements" / "welcome.json", "r") as welcome_file:
    welcome = json.load(welcome_file)
with open(data_directory / "elements" / "success.json", "r") as success_file:
    success = json.load(success_file)
with open(data_directory / "elements" / "introduction.json", "r") as introduction_file:
    introduction = json.load(introduction_file)
with open(data_directory / "template.md", "r") as template_file:
    message_template = template_file.read()

def airtable_filter_formula(field, value):
    return "{" + field.replace("{", r"\{").replace("}", r"\}") + "} = '" + value.replace("'", r"\'").replace("\\", "\\\\") + "'"
def airtable_create_record(table, field, value):
    return airtable_volunteers.create(table, {field: value})["id"]
def airtable_unique_records(table, field, values):
    response=[]
    for value in values:
        existing = airtable_volunteers.get(table, filter_by_formula=airtable_filter_formula(field, value))["records"]
        response += [existing[0]["id"] if existing else airtable_create_record(table, field, value)]
    return list(set(response))

def format_object(object, *arguments, **keyword_arguments):
    """Applies the `str.format()` method to a nested JSON-like object."""
    if type(object) is dict:
        return {
            format_object(key, *arguments, **keyword_arguments):
            format_object(value, *arguments, **keyword_arguments)
            for key, value in object.items()
            }
    elif type(object) is list:
        return [
            format_object(item, *arguments, **keyword_arguments)
            for item in object
            ]
    elif type(object) is str:
        return object.format(*arguments, **keyword_arguments)
    else:
        return object

def handle_form_submission(action):
    def extract(value):
        value = list(value.values())[0]
        if value["type"] == "static_select":
            return [value["selected_option"]["value"]]
        elif value["type"] in ("multi_external_select", "checkboxes"):
            return [item["value"] for item in value["selected_options"]]
        elif value["type"] == "plain_text_input":
            return value["value"]
    state = {
        field: extract(value)
        for field, value in action["view"]["state"]["values"].items()
        }
    user = slack_client.users_info(user=action["user"]["id"])["user"]

    record = airtable_volunteers.create("Volunteers", {
        "Slack Handle": user["profile"]["display_name_normalized"],
        "Slack User ID":  user["id"],

        # "Email": "",
        "Profession": state["profession"],
        "External Organization": state["organization"],
        "Weekly Capacity (new)": int(state["availability"].pop()),

        "Skills": airtable_unique_records("Skills", "Name", state["skills"]),
        "Languages": airtable_unique_records("Languages", "Language", state["languages"]),
        "Industry": airtable_unique_records("Industries", "Name", state["industries"]),
        # "Equipment": "",

        # "City": "",
        # # "Country": "",
        # "State/Province": "",
        # "Zip Code": "",
        # "Geolocation": "",

        "Volunteer Interest": True,
        "Timezone": user["tz_label"],
        "Experience": state["experience"],
        "Management Interest": "leadership" in state["options"],
        "Privacy Policy": "privacy" in state["options"],
        })["id"]
    airtable_mails.create("Email Addresses", {
        "Volunteer Record": record,
        "Email Address": user["profile"]["email"],
    })

    introduction_message = format_object(
        introduction,
        user=action["user"]["id"],
        skills=", ".join(state["skills"] + state["languages"]),
        experience=state["experience"]
        )
    slack_client.chat_postMessage(
        channel="G012HLGCNKY",
        link_names=True,
        text="",
        blocks=introduction_message,
        username=user["profile"]["display_name"],
        icon_url=user["profile"]["image_512"]
        )

    suggestion = ""
    channels = "\n".join(matcher.recommend_channels(model, " ".join(state["skills"])))
    jobs = "\n".join(matcher.recommend_jobs(model, " ".join(state["skills"])))
    if jobs:
        suggestion += "\n*Recommended jobs*\n{}\n".format(jobs)
    message = message_template.format(suggestion=suggestion)

    print(slack_client.chat_postMessage(
        channel=action["user"]["id"],
        link_names=True,
        text=message
        ))

@application.route("/interactivity", methods=["POST"])
def handle_interactivity():
    action = json.loads(request.form["payload"])

    if action["type"] == "shortcut":
        handle_team_join({"event": action}, True)
        return ""

    elif action["type"] == "block_actions":
        if action["actions"][0]["action_id"] == "show_form":
            slack_client.views_open(trigger_id=action["trigger_id"], view=form)
        return ""

    elif action["type"] == "view_submission":
        handle_form_submission(action)
        return success

    else:
        return ""


@slack_event_adapter.on("team_join")
def handle_team_join(event, test=False):
    event = event["event"]
    welcome["blocks"][0]["text"]["text"] = (
        welcome["blocks"][0]["text"]["text"].format(user=event["user"]["id"])
        )
    if test:
        slack_client.chat_postMessage(
            channel=event["user"]["id"],
            link_names=True,
            **welcome,
            text=""
            )
    return make_response("", 200)


# DEPRECATED
@slack_event_adapter.on("message")
def handle_message(event):
    event = event["event"]
    if 'bot_profile' in event:
        return
    if 'thread_ts' in event:
        return
    if 'text' not in event:
        return
    suggestion = ""
    channels = "\n".join(matcher.recommend_channels(model, event["text"]))
    jobs = "\n".join(matcher.recommend_jobs(model, event["text"]))
    if channels:
        suggestion += (
            "\n*Recommended channels*\n" + channels + "\n"
            "(#skill channels have people with similar skills in them; "
            "#discussion channels talk about a topic; #project channels "
            "are working on a project)\n"
            )
    if jobs:
        suggestion += "\n*Recommended jobs*\n{}\n".format(jobs)
    message = message_template.format(suggestion=suggestion)

    print(slack_client.chat_postMessage(
        channel=event["channel"],
        thread_ts=event["ts"],
        link_names=True,
        text=message
        ))
    return


@application.before_request
def skip_retry():
    if int(request.headers.get('X-Slack-Retry-Num', '0')):
        return make_response('', 200)


if __name__ == "__main__":
    application.run(
        host="0.0.0.0",
        port=80
        )
