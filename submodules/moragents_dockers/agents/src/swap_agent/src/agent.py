import json
import requests
from flask import jsonify
from swap_agent.src import tools
from swap_agent.src.config import Config


class SwapAgent:
    def __init__(self, config, llm, llm_ollama, embeddings, flask_app):
        self.llm = llm
        self.flask_app = flask_app
        self.config = config
        self.tools_provided = tools.get_tools()
        self.context = []

    def api_request_url(self, method_name, query_params, chain_id):
        base_url = Config.APIBASEURL + str(chain_id)
        return f"{base_url}{method_name}?{'&'.join([f'{key}={value}' for key, value in query_params.items()])}"

    def check_allowance(self, token_address, wallet_address, chain_id):
        url = self.api_request_url(
            "/approve/allowance",
            {"tokenAddress": token_address, "walletAddress": wallet_address},
            chain_id
        )
        response = requests.get(url, headers=Config.HEADERS)
        data = response.json()
        return data

    def approve_transaction(self, token_address, chain_id, amount=None):
        query_params = {"tokenAddress": token_address, "amount": amount} if amount else {"tokenAddress": token_address}
        url = self.api_request_url("/approve/transaction", query_params, chain_id)
        response = requests.get(url, headers=Config.HEADERS)
        transaction = response.json()
        return transaction

    def build_tx_for_swap(self, swap_params, chain_id):
        url = self.api_request_url("/swap", swap_params, chain_id)
        swap_transaction = requests.get(url, headers=Config.HEADERS).json()
        return swap_transaction

    def get_response(self, message, chain_id, wallet_address):
        prompt = [
            {
                "role": "system",
                "content": (
                    "Don't make assumptions about the value of the arguments for the function "
                    "they should always be supplied by the user and do not alter the value of the arguments. "
                    "Don't make assumptions about what values to plug into functions. Ask for clarification if a user "
                    "request is ambiguous. you only need the value of token1 we dont need the value of token2. After "
                    "starting from scratch do not assume the name of token1 or token2"
                )
            }
        ]
        prompt.extend(message)
        result = self.llm.create_chat_completion(
            messages=prompt,
            tools=self.tools_provided,
            tool_choice="auto",
            temperature=0.01
        )
        if "tool_calls" in result["choices"][0]["message"].keys():
            func = result["choices"][0]["message"]["tool_calls"][0]['function']
            if func["name"] == "swap_agent":
                args = json.loads(func["arguments"])
                tok1 = args["token1"]
                tok2 = args["token2"]
                value = args["value"]
                try:
                    res, role = tools.swap_coins(tok1, tok2, float(value), chain_id, wallet_address)
                except (tools.InsufficientFundsError, tools.TokenNotFoundError, tools.SwapNotPossibleError) as e:
                    self.context = []
                    return str(e), "assistant", None
                return res, role, None
        self.context.append({"role": "assistant", "content": result["choices"][0]["message"]['content']})
        return result["choices"][0]["message"]['content'], "assistant", "crypto swap agent"

    def get_status(self, flag, tx_hash, tx_type):
        response = ''

        if flag == "cancelled":
            response = f"The {tx_type} transaction has been cancelled."
        elif flag == "success":
            response = f"The {tx_type} transaction was successful."
        elif flag == "failed":
            response = f"The {tx_type} transaction has failed."
        elif flag == "initiated":
            response = f"Transaction has been sent, please wait for it to be confirmed."

        if tx_hash:
            response = response + f" The transaction hash is {tx_hash}."

        if flag == "success" and tx_type == "approve":
            response = response + " Please proceed with the swap transaction."
        elif flag != "initiated":
            response = response + " Is there anything else I can help you with?"

        if flag != "initiated":
            self.context = []
            self.context.append({"role": "assistant", "content": response})
            self.context.append({"role": "user", "content": "okay lets start again from scratch"})

        return {"role": "assistant", "content": response}

    def generate_response(self, prompt, chain_id, wallet_address):
        self.context.append(prompt)
        response, role, next_turn_agent = self.get_response(self.context, chain_id, wallet_address)
        return response, role, next_turn_agent

    def chat(self, request):
        try:
            data = request.get_json()
            if 'prompt' in data:
                prompt = data['prompt']
                wallet_address = data['wallet_address']
                chain_id = data['chain_id']
                response, role, next_turn_agent = self.generate_response(prompt, chain_id, wallet_address)
                return {"role": role, "content": response, "next_turn_agent": next_turn_agent}
            else:
                return {"error": "Missing required parameters"}, 400
        except Exception as e:
            return {"Error": str(e)}, 500

    def tx_status(self, request):
        try:
            data = request.get_json()
            if 'status' in data:
                prompt = data['status']
                tx_hash = data.get('tx_hash', '')
                tx_type = data.get('tx_type', '')
                response = self.get_status(prompt, tx_hash, tx_type)
                return response
            else:
                return {"error": "Missing required parameters"}, 400
        except Exception as e:
            return {"Error": str(e)}, 500

    def get_allowance(self, request):
        try:
            data = request.get_json()
            if 'tokenAddress' in data:
                token = data['tokenAddress']
                wallet_address = data['walletAddress']
                chain_id = data["chain_id"]
                res = self.check_allowance(token, wallet_address, chain_id)
                return jsonify({"response": res})
            else:
                return jsonify({"error": "Missing required parameters"}), 400
        except Exception as e:
            return jsonify({"Error": str(e)}), 500

    def approve(self, request):
        try:
            data = request.get_json()
            if 'tokenAddress' in data:
                token = data['tokenAddress']
                chain_id = data['chain_id']
                amount = data['amount']
                res = self.approve_transaction(token, chain_id, amount)
                return jsonify({"response": res})
            else:
                return jsonify({"error": "Missing required parameters"}), 400
        except Exception as e:
            return jsonify({"Error": str(e)}), 500

    def swap(self, request):
        try:
            data = request.get_json()
            if 'src' in data:
                token1 = data['src']
                token2 = data['dst']
                wallet_address = data['walletAddress']
                amount = data['amount']
                slippage = data['slippage']
                chain_id = data['chain_id']
                swap_params = {
                    "src": token1,
                    "dst": token2,
                    "amount": amount,
                    "from": wallet_address,
                    "slippage": slippage,
                    "disableEstimate": False,
                    "allowPartialFill": False,
                }
                swap_transaction = self.build_tx_for_swap(swap_params, chain_id)
                return swap_transaction
            else:
                return jsonify({"error": "Missing required parameters"}), 400
        except Exception as e:
            return jsonify({"Error": str(e)}), 500
