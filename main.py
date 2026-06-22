import base64
with open('drive_token.pickle', 'rb') as f:
    print(base64.b64encode(f.read()).decode())