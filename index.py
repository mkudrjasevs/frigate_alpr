from carmen_cloud_client import VehicleAPIClient, VehicleAPIOptions, SelectedServices, Locations
import paho.mqtt.client as mqtt
import yaml
import json
import logging
import datetime
import sys
import requests

LOG_FILE = 'frigate_alpr.log'
_LOGGER = None
VERSION = '0.1.2'
CURRENT_EVENTS = None # For implementation
mqtt_client = None
# Load configuration
with open("/config/config.yml", "r") as file:
    config = yaml.safe_load(file)

    frigate_url = config['frigate']['url']
    carmen_api_key = config['carmen']['api_key']
    cameras_str = config['frigate']['cameras']
    if cameras_str:
        cameras_list = cameras_str.split(",")
        cameras_list = [cameras.strip() for cameras in cameras_list]
        print("Watched cameras:", cameras_list)
    else:
        _LOGGER.info(f"No watched cameras provided in the config.")

def load_logger():
    global _LOGGER
    _LOGGER = logging.getLogger(__name__)
    _LOGGER.setLevel(config['logging']['log_level'])

    # Create a formatter to customize the log message format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Create a console handler and set the level to display all messages
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)

    # Create a file handler to log messages to a file
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Add the handlers to the logger
    _LOGGER.addHandler(console_handler)
    _LOGGER.addHandler(file_handler)

def run_mqtt_client():
    global mqtt_client
    # Create MQTT configuration variables
    mqtt_broker = config["mqtt"]["broker"]
    mqtt_port = config["mqtt"]["port"]
    mqtt_topic = config["mqtt"]["topic"]
    client_id = config["mqtt"]["client_id"]
    keep_alive = config["mqtt"]["keep_alive"]
    return_topic = config["mqtt"]["return_topic"]
    # Set up MQTT client
    mqtt_client = mqtt.Client(client_id)
    mqtt_client.on_message = on_message  # Attach message handler
    # Connect to the broker
    mqtt_client.connect(mqtt_broker, mqtt_port, keep_alive)
    # Subscribe to the topic
    mqtt_client.subscribe(mqtt_topic)
    # Start the MQTT loop to listen for messages
    _LOGGER.info(f"Listening for messages on topic '{mqtt_topic}'")
    mqtt_client.loop_forever()

## Retrieve event snapshot
def get_snapshot(frigate_event_id, frigate_url, cropped):
    _LOGGER.info(f"Getting snapshot for event: {frigate_event_id}, Crop: {cropped}")
    snapshot_url = f"{frigate_url}/api/events/{frigate_event_id}/snapshot.jpg?crop=1&quality=95"
    _LOGGER.info(f"Snapshot URL: {snapshot_url}, sending for plate recognition")
    payload = {}
    headers = {
        'Accept': 'application/json'
    }
    # get snapshot
    response = requests.request("GET", snapshot_url, headers=headers, data=payload)
    # Check if the request was successful (HTTP status code 200)
    if response.status_code != 200:
        print(f"Error getting snapshot: {response.status_code}")
        return
    return response.content

def get_plate(snapshot):
    options = VehicleAPIOptions(
        api_key=carmen_api_key,
        services=SelectedServices(anpr=True, mmr=True),
        input_image_location=Locations.Europe.Latvia,
        cloud_service_region="EU"
    )
    client = VehicleAPIClient(options)
    response = client.send(snapshot)
    return response

def send_mqtt_message(plate_number, frigate_event_id, frigate_review_id, plate_confidence, vehicle_make, vehicle_model):
    _LOGGER.info(f"Sending message to return topic. Plate: {plate_number}, EventID: {frigate_event_id}, ReviewID: {frigate_review_id}")

    message = {
        'plate_number': str(plate_number).upper(),
        'score': plate_confidence,
        'frigate_event_id': frigate_event_id,
        'frigate_review_id': frigate_review_id,
        'vehicle_make': vehicle_make,
        'vehicle_model': vehicle_model
        }
    return_topic = config['mqtt']['return_topic']

    mqtt_client.publish(return_topic, json.dumps(message))


# Define the callback function for when a message is received
def on_message(client, userdata, message):    
    # Decode the payload and parse it as JSON
    payload = json.loads(message.payload.decode())
    after_data = payload.get('after', {})
    frigate_event_id = after_data['data']['detections'][0]
    frigate_review_id = after_data['id']
    detected_object = after_data['data']['objects'][0]
    camera = after_data['camera']

    ### Is the received object a car? If not, skip
    _LOGGER.debug(f"Received message - eventid {frigate_event_id}, reviewid {frigate_review_id}")
    if camera not in cameras_list:
        _LOGGER.info(f"Camera {camera} not in watched camera list; skipping. ")
        return
    elif detected_object != "car":
        _LOGGER.info(f"Detected object '{detected_object}' is not a car; skipping. ")
        return
    else:
        snapshot = get_snapshot(frigate_event_id, frigate_url, True)
        if not snapshot:
            if frigate_event_id in CURRENT_EVENTS:
                del CURRENT_EVENTS[frigate_event_id] # remove existing id from current events due to snapshot failure - will try again next frame
            return
    
        plate_recogniser_response = get_plate(snapshot)

        if plate_recogniser_response.data.vehicles:
            vehicle = plate_recogniser_response.data.vehicles[0]
            mmr_info = vehicle.mmr
            plate_info = vehicle.plate
            if mmr_info.found == True and plate_info.found == True:
                vehicle_make = mmr_info.make
                vehicle_model = mmr_info.model
                plate_number = vehicle.plate.unicodeText
                make_confidence = mmr_info.makeConfidence
                model_confidence = mmr_info.modelConfidence
                plate_confidence = vehicle.plate.confidence
                _LOGGER.info(f"Detected vehicle: {vehicle_make} {vehicle_model} with licence plate: {plate_number}. Confidence: {make_confidence}, {model_confidence}, {plate_confidence}.")
                send_mqtt_message(plate_number, frigate_event_id, frigate_review_id, plate_confidence, vehicle_make, vehicle_model)
            elif plate_info.found == True and mmr_info.found == False: # Detected plate, but not vehicle make and model (MMR)
                # Access the plate information
                plate_number = vehicle.plate.unicodeText
                plate_confidence = vehicle.plate.confidence
                vehicle_make = 'NONE'
                vehicle_model = 'NONE'
                _LOGGER.info(f"Found plate: {plate_number} with confidence {plate_confidence}. Vehicle make and model was not detected.")
                send_mqtt_message(plate_number, frigate_event_id, frigate_review_id, plate_confidence, vehicle_make, vehicle_model)
            elif mmr_info.found == True and plate_info.found == False: # Detected vehicle make and model, but not plate number
                if mmr_info.make != None:
                    # Access the plate information
                    plate_number = 'NONE'
                    plate_confidence = 'none'
                    vehicle_make = mmr_info.make
                    vehicle_model = mmr_info.model
                    make_confidence = mmr_info.makeConfidence
                    model_confidence = mmr_info.modelConfidence
                    _LOGGER.info(f"Plate not found, but found vehicle: {vehicle_make} {vehicle_model} with confidence {make_confidence}, {model_confidence}")
                    send_mqtt_message(plate_number, frigate_event_id, frigate_review_id, plate_confidence, vehicle_make, vehicle_model)

                else:
                    _LOGGER.info(f"Vehicle and/or plate has not been found! ")
                    plate_number = 'NONE'
                    plate_confidence = 'none'
                    vehicle_make = 'NONE'
                    vehicle_model = 'NONE'
                    send_mqtt_message(plate_number, frigate_event_id, frigate_review_id, plate_confidence, vehicle_make, vehicle_model)
                    return

        else:
            _LOGGER.info(f"Vehicle and/or plate has not been found! ")
            plate_number = 'NONE'
            plate_confidence = 'none'
            vehicle_make = 'NONE'
            vehicle_model = 'NONE'
            send_mqtt_message(plate_number, frigate_event_id, frigate_review_id, plate_confidence, vehicle_make, vehicle_model)
            return

def setup():
    if config['carmen']['api_key']:
        _LOGGER.info(f"Using Carmen ANPR API.")


def main():
    load_logger()
    _LOGGER.debug(f"Python Version: {sys.version}")
    _LOGGER.info(f"Frigate ALPR Version: {VERSION}")

    setup()
    run_mqtt_client()

if __name__ == '__main__':
    main()