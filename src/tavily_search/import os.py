import os
from openai import OpenAI
os.environ['SILICONFLOW_API_KEY'] = 'sk-29iXSiamivVsybYedx2GFJf0I1SOgZisPhYtL6V2JGsN6df4' # 1

client = OpenAI(
    api_key=os.getenv('SILICONFLOW_API_KEY'),
    base_url="https://api.kourichat.com/v1/", # 2
)
completion = client.chat.completions.create(
    model="gemini-2.5-pro", # 3
    messages=[
        {'role': 'system', 'content': 'You are a helpful assistant.'},
        {'role': 'user', 'content': '你是谁？'}]
    )
# print(completion.model_dump_json())
print(completion.choices[0].message.content)