const express = require('express');
const path = require('path');
const cors = require('cors');
const axios = require('axios');

const app = express();
const port = 3001;

// Update this path to your actual frontend folder
const FRONTEND_PATH = 'D:/buddha/frontend';

app.use(cors());
app.use(express.json());
app.use(express.static(FRONTEND_PATH));

// Proxy all API requests to backend
app.get('/api/*', async (req, res) => {
    try {
        const response = await axios.get(`http://localhost:3000${req.originalUrl}`, {
            timeout: 10000
        });
        res.json(response.data);
    } catch (error) {
        console.error('Proxy error:', error.message);
        res.status(500).json({ 
            success: false, 
            error: 'Backend server error',
            flights: []
        });
    }
});

app.post('/api/*', async (req, res) => {
    try {
        const response = await axios.post(`http://localhost:3000${req.originalUrl}`, req.body);
        res.json(response.data);
    } catch (error) {
        res.status(500).json({ success: false, error: error.message });
    }
});

app.get('/', (req, res) => {
    res.sendFile(path.join(FRONTEND_PATH, 'index.html'));
});

app.listen(port, () => {
    console.log(`
    ╔════════════════════════════════════╗
    ║  Frontend Server Running            ║
    ║  http://localhost:${port}           ║
    ║  Proxying API to backend :3000      ║
    ║  Serving from: ${FRONTEND_PATH}     ║
    ╚════════════════════════════════════╝
    `);
});