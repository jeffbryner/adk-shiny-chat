from google.adk.agents.llm_agent import Agent
from google.adk.sessions import InMemorySessionService, Session
from google.adk.memory import InMemoryMemoryService
from .firestore_session_service import FirestoreSessionService


from google.adk.runners import Runner
from google.adk.apps.app import App
import os
import logging

logger = logging.getLogger(__name__)

# set environment variables for Google
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
# os.environ["GOOGLE_CLOUD_PROJECT"] = "your-gcp-project-id"  # Set your GCP project ID
os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"


ice_cream_agent = Agent(
    model="gemini-2.5-flash",
    name="ice_cream_agent",
    description="An agent that talks about ice cream.",
    instruction="You are an ice cream enthusiast. Your only purpose is to talk about ice cream. You can talk about flavors, toppings, or your favorite ice cream shop. You can also ask the user about their favorite ice cream.",
    tools=[],
)

app = App(
    name="ice_cream_agent",
    root_agent=ice_cream_agent,
)

runner = Runner(
    app=app,
    session_service=FirestoreSessionService(),
    memory_service=InMemoryMemoryService(),
)
