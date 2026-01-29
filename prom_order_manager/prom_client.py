import requests
import logging
from config import PROM_API_HOST

class PromClient:
    def __init__(self, token):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        self.host = PROM_API_HOST

    def get_orders(self, status=None):
        """
        Fetch orders from Prom.ua.
        :param status: Filter by status (e.g., 'received', 'processing', 'shipped')
        :return: List of orders
        """
        url = f"{self.host}/orders/list"
        params = {}
        if status:
            params["status"] = status
        
        try:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("orders", [])
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching orders: {e}")
            return []

    def get_order_details(self, order_id):
        """
        Fetch full details for a specific order.
        """
        url = f"{self.host}/orders/{order_id}"
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return data.get("order", {})
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching order {order_id}: {e}")
            return None

    def set_order_status(self, order_id, status):
        """
        Update order status using batch endpoint.
        :param status: 'pending', 'received', 'delivered', 'canceled', 'draft', 'paid'
        """
        url = f"{self.host}/orders/set_status"
        try:
            id_int = int(order_id)
        except ValueError:
            logging.error(f"Invalid order_id {order_id}")
            return False

        body = {
            "status": status,
            "ids": [id_int]
        }
        
        # If status is canceled, we might need a cancellation reason, but for custom status it shouldn't be needed.
        # However, sometimes Prom API returns "not allowed" if transition is invalid.
        # Let's log the full response if it fails.
        
        try:
            response = requests.post(url, headers=self.headers, json=body)
            response.raise_for_status()
            data = response.json()
            
            # Check for errors
            if data.get("errors"):
                logging.error(f"Prom API returned errors: {data['errors']}")
                # If error is 'This status value is not allowed', it usually means invalid transition
                return False
            
            # Check if there are warnings (sometimes it says success but nothing happened)
            if data.get("warnings"):
                 logging.warning(f"Prom API returned warnings: {data['warnings']}")

            return True
        except requests.exceptions.RequestException as e:
            logging.error(f"Error setting status for order {order_id} to {status}: {e}")
            return False

    def get_product(self, product_id):
        """
        Fetch product details to get private notes.
        """
        url = f"{self.host}/products/{product_id}"
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return data.get("product", {})
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching product {product_id}: {e}")
            return None
