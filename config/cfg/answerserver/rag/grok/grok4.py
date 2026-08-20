import logging
import os
import requests
import json



# --- CONSTANTS ---
LOG_DIR = "/opt/answerserver/logs"
API_KEY = "YOUR_REAL_GROK_API_KEY_HERE"  # Replace with your actual API key in production

logger = logging.getLogger("answer-server-rag-logger")
logger.setLevel(logging.DEBUG)  # Changed to DEBUG for more granularity

# --- SETUP LOGGER ---
def setup_logger():
    try:
        if not logger.handlers:
            # Ensure log directory exists
            if not os.path.exists(LOG_DIR):
                os.makedirs(LOG_DIR, exist_ok=True)
            
            log_file = os.path.join(LOG_DIR, "grok4.log")
            
            # File handler
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(
                logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            )
            logger.addHandler(file_handler)
            
            # Console handler for local debugging
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(
                logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            )
            logger.addHandler(console_handler)
            
            logger.debug(f"Logging initialized. File: {log_file}")
        else:
            logger.debug("Logger already has handlers, skipping setup")
    except Exception as e:
        # Fallback to basic console logging if setup fails
        logging.basicConfig(level=logging.DEBUG)
        logger.error(f"Logger setup failed: {str(e)}", exc_info=True)


setup_logger()
logger.info("Logging initialized v5")


# --- Function to generate text using Grok API ---
def generate(prompt: str) -> str:
    """
    Main function to generate text using xAI Grok API with the requests library.
    """
    logger.debug(f"generate called with prompt: {prompt}")
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}"
        }
        logger.debug(f"Headers: {headers}")

        data = {
            "messages": [
                {
                    "role": "system",
                    "content": "Please respond to the question in the prompt as accurately as possible."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "model": "grok-4-1-fast-reasoning",
            "stream": False,
            "temperature": 0,
            "max_tokens": 1000000
        }
        logger.debug(f"Request data: {json.dumps(data, indent=2)}")

        logger.debug("Sending POST request to xAI API")
        response = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=3600  # Added timeout to prevent hanging
        )
        logger.debug(f"Response status code: {response.status_code}")
        logger.debug(f"Response headers: {response.headers}")
        logger.debug(f"Raw response text: {response.text}")

        response.raise_for_status()  # Raises exception for 4xx/5xx errors
        response_json = response.json()
        logger.debug(f"Response JSON: {json.dumps(response_json, indent=2)}")

        # Check if expected keys exist
        if "choices" not in response_json or not response_json["choices"]:
            logger.error("No 'choices' in response or empty choices")
            raise ValueError("Invalid response: No choices found")
        
        content = response_json["choices"][0]["message"]["content"]
        logger.debug(f"Extracted content: {content}")
        return content

    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {str(e)}", exc_info=True)
        raise
    except KeyError as e:
        logger.error(f"Unexpected response structure: {str(e)}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in generate: {str(e)}", exc_info=True)
        raise

# --- Function to get token count ---
def get_token_count(text: str, token_limit: int):
    """
    Function to split the text based on token limit and return token count with logging.
    """
    logger.debug(f"get_token_count called with text: {text[:100]}..., token_limit: {token_limit}")
    try:
        text_split = text.split(' ')
        logger.debug(f"Text split into {len(text_split)} words")
        
        result_text = ' '.join(text_split[:token_limit])
        token_count = len(text_split)
        logger.debug(f"Result text: {result_text[:100]}..., Token count: {token_count}")
        
        return result_text, token_count
    except Exception as e:
        logger.error(f"Error in get_token_count: {str(e)}", exc_info=True)
        raise
    #               ↑↑↑ use env var in production: os.getenv("XAI_API_KEY")
 