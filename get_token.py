import requests
import json

url = "https://accounts.zoho.in/oauth/v2/token"
data = {
    "grant_type": "authorization_code",
    "client_id": "1000.4SHSG6EOHDS5KCFB00LZ0EIEM17QFF",
    "client_secret": "51da0396d33e06229a4102101420a7256a65469c8e",
    "redirect_uri": "http://localhost:8080/callback",
    "code": "1000.ae85ad47eb61cae18f4f990f8830df13.da3c0a4ae97ff34a26932ab1b526ae35"
}

response = requests.post(url, data=data)
print(response.text)
