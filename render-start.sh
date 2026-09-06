#!/bin/bash

echo "Starting Buddha Air Flight Tracker on Render..."

# Set Python path
export PYTHONPATH=$PYTHONPATH:/app

# Start Python Flask API (FlightRadar24 tracker) in background
echo "Starting Python Fleet API on port 5051..."
python3 api_server.py &
PYTHON_PID=$!

# Wait for Python to initialize
sleep 3

# Start Node.js server (main app) - Render will use PORT env var
echo "Starting Node.js server on port $PORT..."
node server.js &
NODE_PID=$!

# Function to handle shutdown
cleanup() {
    echo "Shutting down services..."
    kill $PYTHON_PID 2>/dev/null || true
    kill $NODE_PID 2>/dev/null || true
    exit 0
}

# Trap signals
trap cleanup SIGINT SIGTERM SIGQUIT

# Keep script running
wait