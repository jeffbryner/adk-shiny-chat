import logging
import uuid
from shiny import App, ui
from shinychat import Chat, chat_ui
from google.genai import types
from google.adk.agents.run_config import RunConfig, StreamingMode
from ice_cream_agent.agent import runner

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# UI Definition
app_ui = ui.page_fillable(
    ui.panel_title("Ice Cream Agent 🍦"),
    chat_ui("chat"),
)


# Server Logic
def server(input, output, session):
    # Generate a unique session ID for this browser session
    session_id = str(uuid.uuid4())
    user_id = "user"

    # Initialize Chat instance
    chat = Chat(id="chat")

    # Track if session is created
    session_created = False

    @chat.on_user_submit
    async def handle_user_input(message: str):
        nonlocal session_created
        if not session_created:
            logger.info(f"New ADK session started: {session_id}")
            await runner.session_service.create_session(
                app_name="ice_cream_agent", user_id=user_id, session_id=session_id
            )
            session_created = True

        """
        Handles incoming chat messages and streams the agent responses.
        """
        # Create the content object for the ADK runner
        user_content = types.Content(role="user", parts=[types.Part(text=message)])

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
                    logger.debug(f"Received event: {event}")
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


# Create the Shiny App
# run with shiny run app.py
app = App(app_ui, server)
