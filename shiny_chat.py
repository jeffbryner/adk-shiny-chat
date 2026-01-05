from shiny import (
    App,
    Inputs,
    Outputs,
    Session,
    render,
    ui,
    module,
    render,
    run_app,
)
from google.genai import types
from google.adk.agents.run_config import RunConfig, StreamingMode
from ice_cream_agent.agent import runner
import uuid
import logging


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


## chat module ##
@module.ui
def chat_mod_ui(messages=[]):
    chat_ui = ui.chat_ui(id="chat", messages=messages, height="85vh", fill=True)
    return chat_ui


@module.server
def chat_mod_server(input, output, session, messages):
    chat = ui.Chat(id="chat", messages=messages)
    # Track if session is created in ADK
    session_created = False

    @chat.on_user_submit
    async def _():
        new_message = chat.user_input()
        nonlocal session_created
        if not session_created:
            # Generate a unique session ID for this browser session
            session_id = str(uuid.uuid4())
            user_id = "user"
            logger.info(f"New ADK session started: {session_id}")
            await runner.session_service.create_session(
                app_name="ice_cream_agent", user_id=user_id, session_id=session_id
            )
            session_created = True

        """
        Handles incoming chat messages and streams the agent's response.
        """
        # Create the content object for the ADK runner
        user_content = types.Content(role="user", parts=[types.Part(text=new_message)])

        # Define the stream generator
        async def response_stream():
            try:
                # runner.run_async returns an AsyncGenerator of Events
                async for event in runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=user_content,
                    run_config=RunConfig(streaming_mode=StreamingMode.SSE),
                ):
                    # logger.debug(f"Received event: {event}")
                    # Check if the event has content and parts (text response)
                    # stream only non-final responses
                    if (
                        event.content
                        and event.content.parts
                        and not event.is_final_response()
                    ):
                        for part in event.content.parts:
                            if part.text:
                                yield part.text
            except Exception as e:
                logger.error(f"Error during agent run: {e}")
                yield f"Error: {str(e)}"

        # Stream the response to the chat
        await chat.append_message_stream(response_stream())


## end chat module


# page layout
app_page_chat_ui = ui.page_fluid(
    ui.card(
        ui.card_header("Shiny Chat"),
        ui.output_ui("chat"),
    ),
)


# page logic
def adk_chat_server(input: Inputs, output: Outputs, session: Session):

    @render.ui
    def chat():

        chat_messages = []
        # start the module server
        chat_mod_server("chat_session", messages=chat_messages)
        # start the module UI
        return chat_mod_ui("chat_session", messages=chat_messages)


# allow this to run standalone
# python shiny_chat.py
starlette_app = App(app_page_chat_ui, adk_chat_server)
if __name__ == "__main__":

    run_app(
        "shiny_chat:starlette_app",
        launch_browser=True,
        log_level="debug",
        reload=True,
    )
