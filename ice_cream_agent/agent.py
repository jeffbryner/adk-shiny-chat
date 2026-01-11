from google.adk.agents.llm_agent import Agent
from google.adk.sessions import InMemorySessionService, Session
from google.adk.memory import InMemoryMemoryService
from .firestore_session_service import FirestoreSessionService
from .firestore_llm_memory_service import FirestoreLLMMemoryService
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.adk.tools import function_tool


from google.adk.runners import Runner
from google.adk.apps.app import App
import os
import logging

logger = logging.getLogger(__name__)

# set environment variables for Google
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
# os.environ["GOOGLE_CLOUD_PROJECT"] = "your-gcp-project-id"  # Set your GCP project ID
os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"

# Create a global variable to hold the current session
# It starts empty (None)
from contextvars import ContextVar

current_session_var = ContextVar("current_session", default=None)


# agent callbacks
async def capture_session_context(callback_context: CallbackContext):
    """
    Runs before the agent starts. Grabs the session and stores it
    in a context variable so tools can access it later.
    """
    session = callback_context.session
    if session:
        # Set the global variable for this specific run
        current_session_var.set(session)


async def auto_save_session_to_memory_callback(callback_context: CallbackContext):
    logger.info(f"💾 processing auto save for session: {callback_context.session.id}")
    logger.info(f"{callback_context._invocation_context.memory_service}")

    await callback_context._invocation_context.memory_service.add_session_to_memory(
        session=callback_context.session
    )


# Initialize Firestore memory service globally for tool access
firestore_memory_service = FirestoreLLMMemoryService()


# tool to recall memory
async def recall_memory(query: str) -> str:
    """
    Searches the agent's long-term memory (Firestore) for information
    relevant to the query. Use this to find user preferences, past
    conversations, or facts stored from previous sessions.

    Args:
        query: The specific topic or question to search for (e.g., "favorite ice cream").
    """
    try:
        # Retrieve the session from our global variable
        session = current_session_var.get()
        if not session:
            logger.error("❌ No session found in context variable.")
            return "Error: Could not identify the current user session."

        # Access the global service
        if not firestore_memory_service:
            return "Error: Memory service is not available."

        logger.info(
            f"🔍 Agent is searching memory for: user:{session.user_id} query: '{query}'"
        )

        # Call the search method on your service
        # (Assumes your service has a 'search_memory' or similar method)
        results = await firestore_memory_service.search_memory(
            query=query, app_name=session.app_name, user_id=session.user_id
        )

        if not results:
            return "No relevant memories found."

        # Format results into a string the LLM can read
        formatted_memories = "\n".join(
            [
                f"{r.timestamp}:{r.author} {r.content.parts[0].text}"
                for r in results.memories
            ]
        )
        logger.info(f"🧠 Retrieved memories:\n{formatted_memories}")
        return f"Found the following memories:\n{formatted_memories}"

    except Exception as e:
        return f"Error searching memory: {str(e)}"


recall_memory_tool = function_tool.FunctionTool(func=recall_memory)

ice_cream_agent = Agent(
    model="gemini-2.5-flash",
    name="ice_cream_agent",
    description="An agent that talks about ice cream.",
    instruction="You are an ice cream enthusiast. Your only purpose is to talk about ice cream. You can talk about flavors, toppings, or your favorite ice cream shop. You can also ask the user about their favorite ice cream.",
    tools=[PreloadMemoryTool(), recall_memory],
    before_agent_callback=capture_session_context,
    after_agent_callback=auto_save_session_to_memory_callback,
)

app = App(
    name="ice_cream_agent",
    root_agent=ice_cream_agent,
)

runner = Runner(
    app=app,
    session_service=FirestoreSessionService(),
    memory_service=firestore_memory_service,
)
