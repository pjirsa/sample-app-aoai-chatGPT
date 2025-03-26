import copy
import json
import os
import logging
import uuid
import httpx
import asyncio
from quart import (
    Blueprint,
    Quart,
    jsonify,
    make_response,
    request,
    send_from_directory,
    render_template,
    current_app,
)

# from openai import AsyncAzureOpenAI
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai import PromptExecutionSettings
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion, AzureChatCompletion, AzureChatPromptExecutionSettings
from semantic_kernel.connectors.search_engine import GoogleConnector
from semantic_kernel.contents import ChatHistory
from semantic_kernel.core_plugins import WebSearchEnginePlugin
from semantic_kernel.functions import KernelArguments

from azure.identity.aio import (
    DefaultAzureCredential,
    get_bearer_token_provider
)
from backend.auth.auth_utils import get_authenticated_user_details
from backend.security.ms_defender_utils import get_msdefender_user_json
from backend.history.cosmosdbservice import CosmosConversationClient
from backend.settings import (
    app_settings,
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
)
from backend.utils import (
    format_as_ndjson,
    format_stream_response,
    format_non_streaming_response,
    convert_to_pf_format,
    format_pf_non_streaming_response,
)
import semantic_kernel as sk
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior
from semantic_kernel.connectors.search.google import GoogleSearch
from semantic_kernel.functions import KernelParameterMetadata, KernelPlugin

bp = Blueprint("routes", __name__, static_folder="static",
               template_folder="static")

cosmos_db_ready = asyncio.Event()


def create_app():
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    @app.before_serving
    async def init():
        try:
            app.cosmos_conversation_client = await init_cosmosdb_client()
            cosmos_db_ready.set()
        except Exception as e:
            logging.exception("Failed to initialize CosmosDB client")
            app.cosmos_conversation_client = None
            raise e

    return app


@bp.route("/")
async def index():
    return await render_template(
        "index.html",
        title=app_settings.ui.title,
        favicon=app_settings.ui.favicon
    )


@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")


@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)


# Debug settings
DEBUG = os.environ.get("DEBUG", "false")
if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.DEBUG)

USER_AGENT = "GitHubSampleWebApp/AsyncAzureOpenAI/1.0.0"


# Frontend Settings via Environment Variables
frontend_settings = {
    "auth_enabled": app_settings.base_settings.auth_enabled,
    "feedback_enabled": (
        app_settings.chat_history and
        app_settings.chat_history.enable_feedback
    ),
    "ui": {
        "title": app_settings.ui.title,
        "logo": app_settings.ui.logo,
        "chat_logo": app_settings.ui.chat_logo or app_settings.ui.logo,
        "chat_title": app_settings.ui.chat_title,
        "chat_description": app_settings.ui.chat_description,
        "show_share_button": app_settings.ui.show_share_button,
        "show_chat_history_button": app_settings.ui.show_chat_history_button,
    },
    "sanitize_answer": app_settings.base_settings.sanitize_answer,
    "oyd_enabled": app_settings.base_settings.datasource_type,
}


# Enable Microsoft Defender for Cloud Integration
MS_DEFENDER_ENABLED = os.environ.get(
    "MS_DEFENDER_ENABLED", "true").lower() == "true"

azure_openai_tools = []
azure_openai_available_tools = []


async def openai_remote_azure_function_call(function_name, function_args):
    if app_settings.azure_openai.function_call_azure_functions_enabled is not True:
        return

    azure_functions_tool_url = f"{app_settings.azure_openai.function_call_azure_functions_tool_base_url}?code={app_settings.azure_openai.function_call_azure_functions_tool_key}"
    headers = {'content-type': 'application/json'}
    body = {
        "tool_name": function_name,
        "tool_arguments": json.loads(function_args)
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(azure_functions_tool_url, data=json.dumps(body), headers=headers)
    response.raise_for_status()

    return response.text


async def init_cosmosdb_client():
    cosmos_conversation_client = None
    if app_settings.chat_history:
        try:
            cosmos_endpoint = (
                f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
            )

            if not app_settings.chat_history.account_key:
                async with DefaultAzureCredential() as cred:
                    credential = cred

            else:
                credential = app_settings.chat_history.account_key

            cosmos_conversation_client = CosmosConversationClient(
                cosmosdb_endpoint=cosmos_endpoint,
                credential=credential,
                database_name=app_settings.chat_history.database,
                container_name=app_settings.chat_history.conversations_container,
                enable_message_feedback=app_settings.chat_history.enable_feedback,
            )
        except Exception as e:
            logging.exception("Exception in CosmosDB initialization", e)
            cosmos_conversation_client = None
            raise e
    else:
        logging.debug("CosmosDB not configured")

    return cosmos_conversation_client


async def init_semantic_kernel(selected_model="gpt-4o") -> tuple[Kernel, AzureChatCompletion]:
    """
    Initialize a Semantic Kernel instance with AsyncAzureChatCompletion and Google search.

    Args:
        selected_model (str): The name of the model to use. Default is 'gpt-4o'.

    Returns:
        tuple: (semantic_kernel.Kernel, GoogleSearch) - The initialized Semantic Kernel instance 
               and the Google search plugin.

    Raises:
        Exception: If there are issues with initialization or configuration.
    """
    try:
        # API version check
        if (
            app_settings.azure_openai.preview_api_version
            < MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
        ):
            raise ValueError(
                f"The minimum supported Azure OpenAI preview API version is '{MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION}'"
            )

        # Endpoint
        if (
            not app_settings.azure_openai.endpoint and
            not app_settings.azure_openai.resource
        ):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_RESOURCE is required"
            )

        endpoint = (
            app_settings.azure_openai.endpoint
            if app_settings.azure_openai.endpoint
            else f"https://{app_settings.azure_openai.resource}.openai.azure.com/"
        )

        # Authentication
        aoai_api_key = app_settings.azure_openai.key
        ad_token_provider = None
        if not aoai_api_key:
            logging.debug(
                "No AZURE_OPENAI_KEY found, using Azure Entra ID auth")
            async with DefaultAzureCredential() as credential:
                ad_token_provider = get_bearer_token_provider(
                    credential,
                    "https://cognitiveservices.azure.com/.default"
                )

        # Deployment
        deployment = app_settings.azure_openai.model
        if not deployment:
            raise ValueError("AZURE_OPENAI_MODEL is required")

        # Default Headers
        default_headers = {"x-ms-useragent": USER_AGENT}

        if not endpoint or not deployment:
            raise ValueError(
                "Missing required configuration for Semantic Kernel initialization.")

        # Create a new kernel instance
        kernel = sk.Kernel()

        # Add Async Azure Chat Completion service
        azure_chat_completion = AzureChatCompletion(
            service_id="chat",
            deployment_name=deployment,
            endpoint=endpoint,
            api_key=aoai_api_key,
            api_version=app_settings.azure_openai.preview_api_version
        )

        # Set up default headers in line with existing AsyncAzureOpenAI setup
        default_headers = {"x-ms-useragent": USER_AGENT}
        azure_chat_completion.client.default_headers.update(default_headers)

        # Register the Async Azure Chat Completion service as the default
        kernel.add_service(azure_chat_completion)        

        # Initialize Google Search Plugin
        google_api_key = os.environ.get("GOOGLE_API_KEY")
        google_search_engine_id = os.environ.get("GOOGLE_SEARCH_ENGINE_ID")

        google_plugin = None
        if google_api_key and google_search_engine_id:
            google_search = GoogleSearch(
                api_key=google_api_key,
                search_engine_id=google_search_engine_id
            )

            # Add Google Search as a plugin using KernelPlugin.from_text_search_with_search
            google_plugin = KernelPlugin.from_text_search_with_search(
                google_search,
                plugin_name="WebSearch",
                description="Search the web using Google to get up-to-date information.",
                parameters=[
                    KernelParameterMetadata(
                        name="query",
                        description="The search query.",
                        type="str",
                        is_required=True,
                        type_object=str,
                    ),
                    KernelParameterMetadata(
                        name="top",
                        description="The number of results to return.",
                        type="int",
                        is_required=False,
                        default_value=3,
                        type_object=int,
                    ),
                    KernelParameterMetadata(
                        name="skip",
                        description="The number of results to skip.",
                        type="int",
                        is_required=False,
                        default_value=0,
                        type_object=int,
                    ),
                    KernelParameterMetadata(
                        name="siteSearch",
                        description="Limit search results to specific websites or domains.",
                        type="str",
                        is_required=False,
                        default_value="",
                        type_object=str,
                    )
                ],
            )

            kernel.add_plugin(google_plugin)
            logging.info(
                "Google search plugin registered with Semantic Kernel as WebSearch.")
        else:
            logging.warning(
                "GOOGLE_API_KEY or GOOGLE_SEARCH_ENGINE_ID environment variables not set. Web search functionality will not be available.")

        logging.info(
            f"Semantic Kernel initialized with async model: {deployment}")
        return kernel, azure_chat_completion

    except Exception as e:
        logging.exception("Exception in Semantic Kernel initialization")
        raise e


def prepare_model_args(request_body, request_headers) -> tuple[ChatHistory, AzureChatPromptExecutionSettings]:
    request_messages = request_body.get("messages", [])
    # messages = []
    history = ChatHistory()
    if not app_settings.datasource:
        history.add_system_message(app_settings.azure_openai.system_message)

    for message in request_messages:
        if message:
            match message["role"]:
                case "user":
                    history.add_user_message(message["content"])
                case "assistant" | "function" | "tool":
                    messages_helper = {}
                    messages_helper["role"] = message["role"]
                    messages_helper["content"] = message["content"]
                    history.add_message(messages_helper)

    # user_json = None
    # if (MS_DEFENDER_ENABLED):
    #     authenticated_user_details = get_authenticated_user_details(request_headers)
    #     conversation_id = request_body.get("conversation_id", None)
    #     application_name = app_settings.ui.title
    #     user_json = get_msdefender_user_json(authenticated_user_details, request_headers, conversation_id, application_name)

    execution_settings = AzureChatPromptExecutionSettings(
        service_id="chat",
        max_tokens=app_settings.azure_openai.max_tokens,
        temperature=app_settings.azure_openai.temperature,
        top_p=app_settings.azure_openai.top_p,
        function_choice_behavior=FunctionChoiceBehavior.Auto(auto_invoke=True),
        stop=app_settings.azure_openai.stop_sequence,
    )

    # Replace this section with how Semantic Kernel calls 'on-your-data'
    #         if app_settings.datasource:
    #             model_args["extra_body"] = {
    #                 "data_sources": [
    #                     app_settings.datasource.construct_payload_configuration(
    #                         request=request
    #                     )
    #                 ]
    #             }

    # model_args_clean = copy.deepcopy(model_args)
    # if model_args_clean.get("extra_body"):
    #     secret_params = [
    #         "key",
    #         "connection_string",
    #         "embedding_key",
    #         "encoded_api_key",
    #         "api_key",
    #     ]
    #     for secret_param in secret_params:
    #         if model_args_clean["extra_body"]["data_sources"][0]["parameters"].get(
    #             secret_param
    #         ):
    #             model_args_clean["extra_body"]["data_sources"][0]["parameters"][
    #                 secret_param
    #             ] = "*****"
    #     authentication = model_args_clean["extra_body"]["data_sources"][0][
    #         "parameters"
    #     ].get("authentication", {})
    #     for field in authentication:
    #         if field in secret_params:
    #             model_args_clean["extra_body"]["data_sources"][0]["parameters"][
    #                 "authentication"
    #             ][field] = "*****"
    #     embeddingDependency = model_args_clean["extra_body"]["data_sources"][0][
    #         "parameters"
    #     ].get("embedding_dependency", {})
    #     if "authentication" in embeddingDependency:
    #         for field in embeddingDependency["authentication"]:
    #             if field in secret_params:
    #                 model_args_clean["extra_body"]["data_sources"][0]["parameters"][
    #                     "embedding_dependency"
    #                 ]["authentication"][field] = "*****"

    # logging.debug(f"REQUEST BODY: {json.dumps(model_args_clean, indent=4)}")

    return history, execution_settings


async def promptflow_request(request):
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {app_settings.promptflow.api_key}",
        }
        # Adding timeout for scenarios where response takes longer to come back
        logging.debug(
            f"Setting timeout to {app_settings.promptflow.response_timeout}")
        async with httpx.AsyncClient(
            timeout=float(app_settings.promptflow.response_timeout)
        ) as client:
            pf_formatted_obj = convert_to_pf_format(
                request,
                app_settings.promptflow.request_field_name,
                app_settings.promptflow.response_field_name
            )
            # NOTE: This only support question and chat_history parameters
            # If you need to add more parameters, you need to modify the request body
            response = await client.post(
                app_settings.promptflow.endpoint,
                json={
                    app_settings.promptflow.request_field_name: pf_formatted_obj[-1]["inputs"][app_settings.promptflow.request_field_name],
                    "chat_history": pf_formatted_obj[:-1],
                },
                headers=headers,
            )
        resp = response.json()
        resp["id"] = request["messages"][-1]["id"]
        return resp
    except Exception as e:
        logging.error(
            f"An error occurred while making promptflow_request: {e}")


async def send_chat_request(request_body, request_headers):
    filtered_messages = []
    messages = request_body.get("messages", [])
    for message in messages:
        if message.get("role") != 'tool':
            filtered_messages.append(message)

    request_body['messages'] = filtered_messages
    history, chat_settings = prepare_model_args(request_body, request_headers)

    try:
        # azure_openai_client = await init_openai_client()
        kernel, chat_service = await init_semantic_kernel()
        
        response = chat_service.get_streaming_chat_message_content(history, chat_settings, kernel=kernel)
        # apim_request_id = response.metadata.get("apim-request-id")
        apim_request_id = ''
    except Exception as e:
        logging.exception("Exception in send_chat_request")
        raise e

    return response, apim_request_id


async def complete_chat_request(request_body, request_headers):
    response, apim_request_id = await send_chat_request(request_body, request_headers)
    history_metadata = request_body.get("history_metadata", {})
    non_streaming_response = format_non_streaming_response(
        response, history_metadata, apim_request_id)

    return non_streaming_response


class AzureOpenaiFunctionCallStreamState():
    def __init__(self):
        self.tool_calls = []                # All tool calls detected in the stream
        self.tool_name = ""                 # Tool name being streamed
        self.tool_arguments_stream = ""     # Tool arguments being streamed
        # JSON with the tool name and arguments currently being streamed
        self.current_tool_call = None
        # All function messages to be appended to the chat history
        self.function_messages = []
        # Streaming state (INITIAL, STREAMING, COMPLETED)
        self.streaming_state = "INITIAL"


async def process_function_call_stream(completionChunk, function_call_stream_state, request_body, request_headers, history_metadata, apim_request_id):
    if hasattr(completionChunk, "choices") and len(completionChunk.choices) > 0:
        response_message = completionChunk.choices[0].delta

        # Function calling stream processing
        if response_message.tool_calls and function_call_stream_state.streaming_state in ["INITIAL", "STREAMING"]:
            function_call_stream_state.streaming_state = "STREAMING"
            for tool_call_chunk in response_message.tool_calls:
                # New tool call
                if tool_call_chunk.id:
                    if function_call_stream_state.current_tool_call:
                        function_call_stream_state.tool_arguments_stream += tool_call_chunk.function.arguments if tool_call_chunk.function.arguments else ""
                        function_call_stream_state.current_tool_call[
                            "tool_arguments"] = function_call_stream_state.tool_arguments_stream
                        function_call_stream_state.tool_arguments_stream = ""
                        function_call_stream_state.tool_name = ""
                        function_call_stream_state.tool_calls.append(
                            function_call_stream_state.current_tool_call)

                    function_call_stream_state.current_tool_call = {
                        "tool_id": tool_call_chunk.id,
                        "tool_name": tool_call_chunk.function.name if function_call_stream_state.tool_name == "" else function_call_stream_state.tool_name
                    }
                else:
                    function_call_stream_state.tool_arguments_stream += tool_call_chunk.function.arguments if tool_call_chunk.function.arguments else ""

        # Function call - Streaming completed
        elif response_message.tool_calls is None and function_call_stream_state.streaming_state == "STREAMING":
            function_call_stream_state.current_tool_call[
                "tool_arguments"] = function_call_stream_state.tool_arguments_stream
            function_call_stream_state.tool_calls.append(
                function_call_stream_state.current_tool_call)

            for tool_call in function_call_stream_state.tool_calls:
                tool_response = await openai_remote_azure_function_call(tool_call["tool_name"], tool_call["tool_arguments"])

                function_call_stream_state.function_messages.append({
                    "role": "assistant",
                    "function_call": {
                        "name": tool_call["tool_name"],
                        "arguments": tool_call["tool_arguments"]
                    },
                    "content": None
                })
                function_call_stream_state.function_messages.append({
                    "tool_call_id": tool_call["tool_id"],
                    "role": "function",
                    "name": tool_call["tool_name"],
                    "content": tool_response,
                })

            function_call_stream_state.streaming_state = "COMPLETED"
            return function_call_stream_state.streaming_state

        else:
            return function_call_stream_state.streaming_state


async def stream_chat_request(request_body, request_headers):
    response, apim_request_id = await send_chat_request(request_body, request_headers)
    history_metadata = request_body.get("history_metadata", {})

    async def generate(apim_request_id, history_metadata):
        async for completionChunk in response:
            yield format_stream_response(completionChunk, history_metadata, apim_request_id)

    return generate(apim_request_id=apim_request_id, history_metadata=history_metadata)


async def conversation_internal(request_body, request_headers):
    try:
        if app_settings.azure_openai.stream and not app_settings.base_settings.use_promptflow:
            result = await stream_chat_request(request_body, request_headers)
            response = await make_response(format_as_ndjson(result))
            response.timeout = None
            response.mimetype = "application/json-lines"
            return response
        else:
            result = await complete_chat_request(request_body, request_headers)
            return jsonify(result)

    except Exception as ex:
        logging.exception(ex)
        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500


@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    return await conversation_internal(request_json, request.headers)


@bp.route("/frontend_settings", methods=["GET"])
def get_frontend_settings():
    try:
        return jsonify(frontend_settings), 200
    except Exception as e:
        logging.exception("Exception in /frontend_settings")
        return jsonify({"error": str(e)}), 500


## Conversation History API ##
@bp.route("/history/generate", methods=["POST"])
async def add_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        history_metadata = {}
        if not conversation_id:
            title = await generate_title(request_json["messages"])
            conversation_dict = await current_app.cosmos_conversation_client.create_conversation(
                user_id=user_id, title=title
            )
            conversation_id = conversation_dict["id"]
            history_metadata["title"] = title
            history_metadata["date"] = conversation_dict["createdAt"]

        # Format the incoming message object in the "chat/completions" messages format
        # then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "user":
            createdMessageValue = await current_app.cosmos_conversation_client.create_message(
                uuid=str(uuid.uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
            if createdMessageValue == "Conversation not found":
                raise Exception(
                    "Conversation not found for the given conversation ID: "
                    + conversation_id
                    + "."
                )
        else:
            raise Exception("No user message found")

        # Submit request to Chat Completions for response
        request_body = await request.get_json()
        history_metadata["conversation_id"] = conversation_id
        request_body["history_metadata"] = history_metadata
        return await conversation_internal(request_body, request.headers)

    except Exception as e:
        logging.exception("Exception in /history/generate")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/update", methods=["POST"])
async def update_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        if not conversation_id:
            raise Exception("No conversation_id found")

        # Format the incoming message object in the "chat/completions" messages format
        # then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "assistant":
            if len(messages) > 1 and messages[-2].get("role", None) == "tool":
                # write the tool message first
                await current_app.cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    input_message=messages[-2],
                )
            # write the assistant message
            await current_app.cosmos_conversation_client.create_message(
                uuid=messages[-1]["id"],
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
        else:
            raise Exception("No bot messages found")

        # Submit request to Chat Completions for response
        response = {"success": True}
        return jsonify(response), 200

    except Exception as e:
        logging.exception("Exception in /history/update")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/message_feedback", methods=["POST"])
async def update_message():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for message_id
    request_json = await request.get_json()
    message_id = request_json.get("message_id", None)
    message_feedback = request_json.get("message_feedback", None)
    try:
        if not message_id:
            return jsonify({"error": "message_id is required"}), 400

        if not message_feedback:
            return jsonify({"error": "message_feedback is required"}), 400

        # update the message in cosmos
        updated_message = await current_app.cosmos_conversation_client.update_message_feedback(
            user_id, message_id, message_feedback
        )
        if updated_message:
            return (
                jsonify(
                    {
                        "message": f"Successfully updated message with feedback {message_feedback}",
                        "message_id": message_id,
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "error": f"Unable to update message {message_id}. It either does not exist or the user does not have access to it."
                    }
                ),
                404,
            )

    except Exception as e:
        logging.exception("Exception in /history/message_feedback")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/delete", methods=["DELETE"])
async def delete_conversation():
    await cosmos_db_ready.wait()
    # get the user id from the request headers
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # delete the conversation messages from cosmos first
        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        # Now delete the conversation
        deleted_conversation = await current_app.cosmos_conversation_client.delete_conversation(
            user_id, conversation_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted conversation and messages",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/delete")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/list", methods=["GET"])
async def list_conversations():
    await cosmos_db_ready.wait()
    offset = request.args.get("offset", 0)
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    # get the conversations from cosmos
    conversations = await current_app.cosmos_conversation_client.get_conversations(
        user_id, offset=offset, limit=25
    )
    if not isinstance(conversations, list):
        return jsonify({"error": f"No conversations for {user_id} were found"}), 404

    # return the conversation ids

    return jsonify(conversations), 200


@bp.route("/history/read", methods=["POST"])
async def get_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    # make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    # get the conversation object and the related messages from cosmos
    conversation = await current_app.cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    # return the conversation id and the messages in the bot frontend format
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    # get the messages for the conversation from cosmos
    conversation_messages = await current_app.cosmos_conversation_client.get_messages(
        user_id, conversation_id
    )

    # format the messages in the bot frontend format
    messages = [
        {
            "id": msg["id"],
            "role": msg["role"],
            "content": msg["content"],
            "createdAt": msg["createdAt"],
            "feedback": msg.get("feedback"),
        }
        for msg in conversation_messages
    ]

    return jsonify({"conversation_id": conversation_id, "messages": messages}), 200


@bp.route("/history/rename", methods=["POST"])
async def rename_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    # make sure cosmos is configured
    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    # get the conversation from cosmos
    conversation = await current_app.cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    # update the title
    title = request_json.get("title", None)
    if not title:
        return jsonify({"error": "title is required"}), 400
    conversation["title"] = title
    updated_conversation = await current_app.cosmos_conversation_client.upsert_conversation(
        conversation
    )

    return jsonify(updated_conversation), 200


@bp.route("/history/delete_all", methods=["DELETE"])
async def delete_all_conversations():
    await cosmos_db_ready.wait()
    # get the user id from the request headers
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # get conversations for user
    try:
        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        conversations = await current_app.cosmos_conversation_client.get_conversations(
            user_id, offset=0, limit=None
        )
        if not conversations:
            return jsonify({"error": f"No conversations for {user_id} were found"}), 404

        # delete each conversation
        for conversation in conversations:
            # delete the conversation messages from cosmos first
            deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
                conversation["id"], user_id
            )

            # Now delete the conversation
            deleted_conversation = await current_app.cosmos_conversation_client.delete_conversation(
                user_id, conversation["id"]
            )
        return (
            jsonify(
                {
                    "message": f"Successfully deleted conversation and messages for user {user_id}"
                }
            ),
            200,
        )

    except Exception as e:
        logging.exception("Exception in /history/delete_all")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/clear", methods=["POST"])
async def clear_messages():
    await cosmos_db_ready.wait()
    # get the user id from the request headers
    authenticated_user = get_authenticated_user_details(
        request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        # make sure cosmos is configured
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # delete the conversation messages from cosmos
        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted messages in conversation",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/clear_messages")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/ensure", methods=["GET"])
async def ensure_cosmos():
    await cosmos_db_ready.wait()
    if not app_settings.chat_history:
        return jsonify({"error": "CosmosDB is not configured"}), 404

    try:
        success, err = await current_app.cosmos_conversation_client.ensure()
        if not current_app.cosmos_conversation_client or not success:
            if err:
                return jsonify({"error": err}), 422
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500

        return jsonify({"message": "CosmosDB is configured and working"}), 200
    except Exception as e:
        logging.exception("Exception in /history/ensure")
        cosmos_exception = str(e)
        if "Invalid credentials" in cosmos_exception:
            return jsonify({"error": cosmos_exception}), 401
        elif "Invalid CosmosDB database name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception} {app_settings.chat_history.database} for account {app_settings.chat_history.account}"
                    }
                ),
                422,
            )
        elif "Invalid CosmosDB container name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception}: {app_settings.chat_history.conversations_container}"
                    }
                ),
                422,
            )
        else:
            return jsonify({"error": "CosmosDB is not working"}), 500


async def generate_title(conversation_messages) -> str:
    # make sure the messages are sorted by _ts descending
    title_prompt = "Summarize the conversation so far into a 4-word or less title. Do not use any quotation marks or punctuation. Do not include any other commentary or description."
    messages = ChatHistory()
    for msg in conversation_messages:
        messages.add_message({"role": msg["role"], "content": msg["content"]})

    try:
        kernel, chat_client = await init_semantic_kernel()
        settings = AzureChatPromptExecutionSettings(
            service_id="title",
            temperature=1,
            max_tokens=64,
        )
        # azure_openai_client = await init_openai_client()
        response = await chat_client.get_chat_message_content(
            messages, settings
        )

        title = response.items[0].to_chat_message_content().content
        return title
    except Exception as e:
        logging.exception("Exception while generating title", e)
        return messages[-2]["content"]


app = create_app()
