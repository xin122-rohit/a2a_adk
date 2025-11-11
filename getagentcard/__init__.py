import azure.functions as func
import json
import os

def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == 'GET' and '/.well-known/agent-card.json' in req.url:
        # Load your agent-card.json (place it in the project root or as a string)
        card_path = os.path.join(os.path.dirname(__file__), '..', 'agent-card.json')
        with open(card_path, 'r') as f:
            card = json.load(f)
        return func.HttpResponse(json.dumps(card), mimetype='application/json', status_code=200)
    return func.HttpResponse("Not Found", status_code=404)