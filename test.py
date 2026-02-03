from google import genai
from google.genai import types
import base64
from google.genai import errors
import os

GOOGLE_PROJECT_ID = "eval-lab"

def generate():
  client = genai.Client(
    vertexai=True,
    project = GOOGLE_PROJECT_ID,
    location="global",
    http_options={
        'base_url': "http://127.0.0.1/proxy",
        # You can also include custom headers if needed by your gateway
        # 'headers': {
        #     'Authorization': 'Bearer YOUR_TOKEN',
        #     'x-api-key': 'YOUR_API_KEY'
        # }
    }
  )


  model = "gemini-3-flash-preview"
  contents = [
    types.Content(
      role="user",
      parts=[
        types.Part.from_text(text="""What is the secret of life?""")
      ]
    )
  ]

  generate_content_config = types.GenerateContentConfig(
    temperature = 1,
    top_p = 0.95,
    seed = 0,
    max_output_tokens = 65535,
    safety_settings = [types.SafetySetting(
      category="HARM_CATEGORY_HATE_SPEECH",
      threshold="OFF"
    ),types.SafetySetting(
      category="HARM_CATEGORY_DANGEROUS_CONTENT",
      threshold="OFF"
    ),types.SafetySetting(
      category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
      threshold="OFF"
    ),types.SafetySetting(
      category="HARM_CATEGORY_HARASSMENT",
      threshold="OFF"
    )],
    thinking_config=types.ThinkingConfig(
      thinking_level="HIGH",
    ),
  )

  try:
    response = client.models.generate_content_stream(
      model = model,
      contents = contents,
      config = generate_content_config,
    )
    for chunk in response:
      print(chunk.text, end="")
  except errors.ClientError as e:
    if "429 Too Many Requests" in str(e):
      print(f"Failed after multiple retries due to Quota Exceeded (429): {e}")
    else:
      print(f"ClientError occurred: {e}")
  except errors.ServerError as e:
    print(f"Failed after multiple retries due to Server Error: {e}")
  except Exception as e:
    print(f"An unexpected error occurred: {e}")

generate()