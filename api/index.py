import sys
import os

# Add the root directory to the python path so it can import the Flask app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
