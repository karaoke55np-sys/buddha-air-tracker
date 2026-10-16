const express = require('express');
const cors = require('cors');
const axios = require('axios');
const cheerio = require('cheerio');
const path = require('path');
const http = require('http');
const socketIo = require('socket.io');
const fs = require('fs');
const { spawn } = require('child_process');

const app = express();
const server = http.createServer(app);
const io = socketIo(server, {
    cors: { origin: "*" },
    transports: ['websocket', 'polling']
});

// ==================== RENDER CONFIGURATION ====================
const PORT = process.env.PORT || 3000;
const FLEET_API_URL = process.env.FLEET_API_URL || 'http://localhost:5051/api/fleet-status';
const IS_RENDER = process.env.RENDER === 'true' || process.env.NODE_ENV === 'production';

app.use(cors());
app.use(express.json());
app.use(express.static(__dirname));

// ==================== CHAT DATA PERSISTENCE ====================
const DATA_FILE = path.join(__dirname, 'chat-data.json');
let users = [];
let messages = [];

const defaultUsers = [
    { id: 1, username: 'admin', password: '1221', displayName: 'Admin', isAdmin: true, createdAt: new Date().toISOString() },
    { id: 2, username: 'testuser', password: '1234', displayName: 'Test User', isAdmin: false, createdAt: new Date().toISOString() },
    { id: 3, username: 'technician', password: 'tech123', displayName: 'Technician', isAdmin: false, createdAt: new Date().toISOString() }
];

function loadData() {
    try {
        if (fs.existsSync(DATA_FILE)) {
            const data = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
            users = data.users || [];
            messages = data.messages || [];
            
            const hasAdmin = users.some(u => u.username === 'admin');
            if (!hasAdmin) {
                users.push(...defaultUsers);
                saveData();
            }
            console.log('📁 Loaded chat data from file');
        } else {
            users = [...defaultUsers];
            messages = [];
            saveData();
            console.log('📁 Created new data file with default users');
        }
    } catch (e) { 
        console.error('Error loading data:', e);
        users = [...defaultUsers];
        saveData();
    }
}

function saveData() {
    try {
        fs.writeFileSync(DATA_FILE, JSON.stringify({ users, messages }, null, 2));
    } catch (e) { 
        console.error('Error saving data:', e);
    }
}

loadData();

// ==================== AUTO-START THE PYTHON FLEET TRACKER ====================
const PYTHON_SCRIPT = path.join(__dirname, 'api_server.py');
let fleetProcess = null;
let shuttingDown = false;
let restartDelayMs = 5000;          // grows on repeated quick crashes, max 60s
let fleetStartedAt = 0;
let restartTimer = null;

function scheduleFleetRestart(reason) {
    if (shuttingDown || restartTimer) return;
    console.error(`⚠️  Fleet tracker ${reason}. Restarting in ${Math.round(restartDelayMs / 1000)}s...`);
    restartTimer = setTimeout(() => {
        restartTimer = null;
        startFleetTracker();
    }, restartDelayMs);
    restartDelayMs = Math.min(restartDelayMs * 2, 60000);
}

function startFleetTracker() {
    if (shuttingDown) return;
    if (!fs.existsSync(PYTHON_SCRIPT)) {
        console.warn(`⚠️  api_server.py not found at ${PYTHON_SCRIPT} -- Live Tracker tab will be unavailable.`);
        return;
    }

    // Use python3 on Render/Linux, python on Windows
    const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
    
    console.log(`🐍 Starting fleet tracker: ${pythonCmd} "${PYTHON_SCRIPT}" on port 5051...`);
    
    const child = spawn(pythonCmd, [PYTHON_SCRIPT], {
        cwd: __dirname,
        env: {
            ...process.env,
            PYTHON_PORT: '5051',
            PYTHONUNBUFFERED: '1'
        }
    });
    fleetProcess = child;
    fleetStartedAt = Date.now();

    child.stdout.on('data', (data) => {
        data.toString().split('\n').filter(Boolean).forEach(line => console.log(`[fleet-api] ${line}`));
    });
    child.stderr.on('data', (data) => {
        data.toString().split('\n').filter(Boolean).forEach(line => console.error(`[fleet-api] ${line}`));
    });

    child.on('error', (err) => {
        // Usually "python not found". Keep retrying (slowly) rather than
        // leaving the Live Tracker dead until someone restarts Node.
        fleetProcess = null;
        scheduleFleetRestart(`could not start (${err.message})`);
    });

    child.on('exit', (code, signal) => {
        fleetProcess = null;
        if (shuttingDown) return;
        // Restart on ANY unexpected exit -- including being killed by a
        // signal (e.g. out of memory), where `code` is null. Previously
        // only a non-zero exit code was restarted, so a killed tracker
        // stayed dead for good.
        if (Date.now() - fleetStartedAt > 5 * 60 * 1000) restartDelayMs = 5000;
        scheduleFleetRestart(`stopped (code ${code}, signal ${signal})`);
    });
}

// Start the Python tracker with a small delay
setTimeout(startFleetTracker, 3000);

function stopFleetTracker() {
    shuttingDown = true;
    if (fleetProcess) {
        console.log('🐍 Stopping fleet tracker...');
        fleetProcess.kill();
    }
}
process.on('SIGINT', () => { stopFleetTracker(); process.exit(0); });
process.on('SIGTERM', () => { stopFleetTracker(); process.exit(0); });
process.on('exit', stopFleetTracker);

// ==================== SOCKET.IO CHAT ====================
io.on('connection', (socket) => {
    console.log('🔌 User connected:', socket.id);
    
    socket.on('user-login', (data) => {
        const { username, password } = data;
        console.log(`📝 Login attempt: ${username}`);
        
        const user = users.find(u => u.username === username);
        
        if (!user) {
            socket.emit('login-error', `User "${username}" not found. Please register first.`);
            return;
        }
        
        if (user.password !== password) {
            socket.emit('login-error', `Wrong password for "${username}". Use "Forgot Password" to reset.`);
            return;
        }
        
        console.log(`✅ Login successful: ${user.displayName}`);
        
        socket.emit('login-success', {
            id: user.id,
            username: user.username,
            displayName: user.displayName,
            isAdmin: user.isAdmin
        });
        socket.broadcast.emit('user-online', user.displayName);
    });
    
    socket.on('user-register', (data) => {
        const { username, password, displayName } = data;
        console.log(`📝 Register attempt: ${username}`);
        
        if (users.find(u => u.username === username)) {
            socket.emit('register-error', 'Username already exists. Please choose another.');
            return;
        }
        
        const newUser = {
            id: Date.now(),
            username: username,
            password: password,
            displayName: displayName || username,
            isAdmin: false,
            createdAt: new Date().toISOString()
        };
        
        users.push(newUser);
        saveData();
        
        console.log(`✅ New user registered: ${displayName || username}`);
        socket.emit('register-success', 'Registration successful! Please login.');
    });
    
    socket.on('forgot-password', (data) => {
        const { username } = data;
        console.log(`📝 Password reset requested for: ${username}`);
        
        const user = users.find(u => u.username === username);
        
        if (user) {
            socket.emit('reset-allowed', { 
                userId: user.id, 
                username: user.username,
                displayName: user.displayName 
            });
            console.log(`✅ Reset allowed for: ${user.displayName}`);
        } else {
            socket.emit('reset-error', `Username "${username}" not found. Please register first.`);
        }
    });
    
    socket.on('reset-password-direct', (data) => {
        const { userId, newPassword } = data;
        console.log(`📝 Direct password reset for user ID: ${userId}`);
        
        const user = users.find(u => u.id === userId);
        
        if (user) {
            user.password = newPassword;
            saveData();
            socket.emit('password-reset-success', 'Password reset successful! You can now login with your new password.');
            console.log(`✅ Password reset for: ${user.displayName}`);
        } else {
            socket.emit('reset-error', 'User not found');
        }
    });
    
    socket.on('get-messages', () => {
        socket.emit('messages-history', messages);
    });
    
    socket.on('send-message', (data) => {
        const { text, userName, userId, groupId, replyTo } = data;
        console.log(`📤 New message from ${userName}: ${text.substring(0, 30)}`);
        
        const newMessage = {
            id: Date.now(),
            text: text,
            userName: userName,
            userId: userId,
            timestamp: new Date().toISOString(),
            groupId: groupId || 'general'
        };
        
        if (replyTo) {
            newMessage.replyTo = {
                id: replyTo.id,
                text: replyTo.text,
                userName: replyTo.userName
            };
            console.log(`↳ Replying to: ${replyTo.userName}`);
        }
        
        messages.push(newMessage);
        if (messages.length > 500) messages.shift();
        saveData();
        io.emit('new-message', newMessage);
    });
    
    socket.on('edit-message', (data) => {
        const { messageId, userId, newText } = data;
        console.log(`✏️ Edit message request: ${messageId} by user ${userId}`);
        
        const user = users.find(u => u.id === userId);
        const message = messages.find(m => m.id === messageId);
        
        if (message && (message.userId === userId || user?.isAdmin)) {
            message.text = newText;
            message.edited = true;
            message.editedAt = new Date().toISOString();
            saveData();
            io.emit('message-edited', { messageId, newText, editedAt: message.editedAt });
            console.log(`✅ Message edited`);
        }
    });
    
    socket.on('delete-message', (data) => {
        const { messageId, userId } = data;
        console.log(`🗑️ Delete message request: ${messageId} by user ${userId}`);
        
        const user = users.find(u => u.id === userId);
        const messageIndex = messages.findIndex(m => m.id === messageId);
        
        if (messageIndex !== -1) {
            const message = messages[messageIndex];
            if (message.userId === userId || user?.isAdmin) {
                messages.splice(messageIndex, 1);
                saveData();
                io.emit('message-deleted', { messageId });
                console.log(`✅ Message deleted`);
            }
        }
    });
    
    socket.on('typing', (data) => {
        socket.broadcast.emit('user-typing', { userName: data.userName, isTyping: data.isTyping });
    });
    
    socket.on('disconnect', () => {
        console.log('🔌 User disconnected:', socket.id);
    });
});

// ==================== DESTINATION IATA CODES ====================
const DESTINATION_IATA = {
    POKHARA: 'PHH', BHAIRAHAWA: 'BWA', BIRATNAGAR: 'BIR', BHARATPUR: 'BHR',
    SIMARA: 'SIF', BHADRAPUR: 'BDP', NEPALGUNJ: 'KEP', JANAKPUR: 'JKR',
    DHANGADHI: 'DHI', SURKHET: 'SKH', RAJBIRAJ: 'RJB', TUMLINGTAR: 'TMI',
    VARANASI: 'VNS', KOLKATA: 'CCU',
};

function extractDigits(s) {
    return (s || '').replace(/\D/g, '');
}

// ==================== FLIGHT RADAR CROSS-CHECK ====================
async function crossCheckWithFlightRadar(flights) {
    let air = [];
    try {
        const res = await axios.get(FLEET_API_URL, { timeout: 8000 });
        air = (res.data && res.data.air) || [];
    } catch (e) {
        console.log(`   ⚠️  Could not cross-check with FlightRadar24 (${e.code || e.message}) -- using desk board status only.`);
        return flights;
    }

    for (const f of flights) {
        const fnDigits = extractDigits(f.flightNumber);
        if (!fnDigits) continue;
        const match = air.find(a => a.callsign && extractDigits(a.callsign) === fnDigits);
        if (match) {
            f.status = 'Departed';
            f.statusDisplay = 'DEPARTED';
            f.remarks = `Airborne now, confirmed via FlightRadar24 (${match.sector_label})`;
            f.flightRadarConfirmed = true;
        }
    }
    return flights;
}

// ==================== FLIGHT DATA SCRAPER ====================
let cachedFlights = [];

async function fetchFlights() {
    try {
        console.log('\n✈️ Fetching live flight data from Buddha Air desk...');
        const response = await axios.get('https://desk.buddhaair.com/station/ktm', {
            headers: { 
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9'
            },
            timeout: 15000
        });
        
        const $ = cheerio.load(response.data);
        const flights = [];

        // Parse gate banner
        const bodyText = $('body').text().replace(/\s+/g, ' ');
        const destPattern = Object.keys(DESTINATION_IATA).join('|');
        const gateBannerRegex = new RegExp(`GATE\\s*(\\d+)\\s*(${destPattern})\\s+(\\d+)`, 'i');
        const gateMatch = bodyText.match(gateBannerRegex);

        let currentBoardingGate = null;
        let currentBoardingFlightDigits = null;
        if (gateMatch) {
            currentBoardingGate = gateMatch[1];
            currentBoardingFlightDigits = extractDigits(gateMatch[3]);
            console.log(`   🟡 Gate banner: Gate ${currentBoardingGate}, flight ${gateMatch[3]} to ${gateMatch[2]} is actually boarding now`);
        }
        
        $('table tbody tr, table tr').each((index, element) => {
            const columns = $(element).find('td');
            
            if (columns.length >= 5) {
                const time = $(columns[0]).text().trim();
                const destination = $(columns[1]).text().trim();
                const flightNumberRaw = $(columns[2]).text().trim();
                let statusText = $(columns[3]).text().trim();
                const remarks = $(columns[4]).text().trim();
                
                if (time && destination && flightNumberRaw) {
                    const bgColor = $(element).attr('bgcolor') || '';
                    const style = $(element).attr('style') || '';
                    const isYellow = bgColor === '#FFFF00' || bgColor === '#FFD700' || 
                                   style.includes('#FFFF00') || style.includes('#FFD700');
                    
                    const remarksLower = remarks.toLowerCase();
                    const statusLower = statusText.toLowerCase();

                    const isNextBoardingText = statusLower.includes('next') && statusLower.includes('boarding');
                    const isActiveBoardingText = !isNextBoardingText &&
                        (remarksLower.includes('boarding') || statusLower.includes('boarding'));

                    const matchesGateBanner = currentBoardingFlightDigits &&
                        extractDigits(flightNumberRaw) === currentBoardingFlightDigits;
                    
                    let finalStatus = 'On Time';
                    let statusDisplay = 'On Time';
                    
                    if (matchesGateBanner || isYellow || isActiveBoardingText) {
                        finalStatus = 'Boarding';
                        statusDisplay = 'BOARDING';
                        console.log(`   🟡 BOARDING: ${flightNumberRaw} to ${destination} at ${time}`);
                    }
                    else if (isNextBoardingText) {
                        finalStatus = 'Next';
                        statusDisplay = 'UP NEXT';
                    }
                    else if (statusText === 'Revised' || statusLower === 'revised' || remarksLower.includes('revised')) {
                        finalStatus = 'Revised';
                        statusDisplay = 'REVISED';
                        console.log(`   🔄 REVISED: ${flightNumberRaw} to ${destination}`);
                    }
                    else if (statusText === 'Delayed' || statusLower === 'delayed' || remarksLower.includes('delay')) {
                        finalStatus = 'Delayed';
                        statusDisplay = 'DELAYED';
                        console.log(`   ⏰ DELAYED: ${flightNumberRaw} to ${destination}`);
                    }
                    
                    let gate = 'TBD';
                    const gateFieldMatch = remarks.match(/GATE\s*(\d+)/i);
                    if (gateFieldMatch) gate = gateFieldMatch[1];
                    else if (matchesGateBanner && currentBoardingGate) gate = currentBoardingGate;
                    
                    flights.push({
                        id: `BUD${flightNumberRaw}`,
                        flightNumber: `BUD ${flightNumberRaw}`,
                        time: time,
                        destination: destination,
                        destinationCode: DESTINATION_IATA[destination.toUpperCase()] || destination.substring(0, 3).toUpperCase(),
                        route: `KTM → ${destination}`,
                        status: finalStatus,
                        statusDisplay: statusDisplay,
                        remarks: remarks || (finalStatus === 'Boarding' ? 'NOW BOARDING - Please proceed to gate' : 'Scheduled'),
                        gate: gate,
                        lastUpdated: new Date().toISOString()
                    });
                }
            }
        });
        
        await crossCheckWithFlightRadar(flights);

        flights.sort((a, b) => {
            const rank = s => (s === 'Boarding' ? 0 : s === 'Departed' ? 2 : 1);
            const ra = rank(a.status), rb = rank(b.status);
            if (ra !== rb) return ra - rb;
            return a.time.localeCompare(b.time);
        });
        
        cachedFlights = flights;
        
        console.log(`\n📊 FLIGHT STATUS SUMMARY:`);
        console.log(`   ✈️ Total Flights: ${flights.length}`);
        console.log(`   🟡 Boarding: ${flights.filter(f => f.status === 'Boarding').length}`);
        console.log(`   🔄 Revised: ${flights.filter(f => f.status === 'Revised').length}`);
        console.log(`   ⏰ Delayed: ${flights.filter(f => f.status === 'Delayed').length}`);
        console.log(`   ✅ On Time: ${flights.filter(f => f.status === 'On Time').length}`);
        
        return flights;
        
    } catch (error) {
        console.error('❌ Error fetching flights:', error.message);
        return [];
    }
}

// ==================== API ROUTES ====================
app.get('/api/flights', async (req, res) => {
    if (req.query.refresh === 'true') {
        await fetchFlights();
    }
    
    const stats = {
        total: cachedFlights.length,
        boarding: cachedFlights.filter(f => f.status === 'Boarding').length,
        onTime: cachedFlights.filter(f => f.status === 'On Time').length,
        revised: cachedFlights.filter(f => f.status === 'Revised' || f.status === 'Delayed').length
    };
    
    res.json({
        success: true,
        flights: cachedFlights,
        stats: stats,
        lastUpdated: new Date().toISOString(),
        count: cachedFlights.length
    });
});

// Last good answer from the Python tracker. While Python is restarting,
// the page keeps getting this (marked as not fresh) instead of an empty
// fleet -- an empty answer is what made every card vanish and the whole
// picture start again from zero.
let lastFleetData = null;

app.get('/api/fleet-status', async (req, res) => {
    try {
        const response = await axios.get(FLEET_API_URL, { timeout: 8000 });
        const data = response.data || {};
        if (Array.isArray(data.records) && data.records.length > 0) lastFleetData = data;
        res.json(data);
    } catch (error) {
        const errDetail = error.code || error.message || (error.response && `HTTP ${error.response.status}`) || 'unknown error';
        console.error(`⚠️  Live tracker proxy error [${errDetail}] -- is 'python api_server.py' running on port 5051?`);
        if (lastFleetData) {
            return res.json({
                ...lastFleetData,
                error: 'Live tracker is restarting -- showing the last known fleet data until it is back.',
            });
        }
        res.status(502).json({
            updated: null,
            counts: { air: 0, ground: 0 },
            records: [], air: [], ground: [],
            error: `Could not reach live tracker (api_server.py) at ${FLEET_API_URL} -- is it running?`
        });
    }
});

app.post('/api/refresh', async (req, res) => {
    await fetchFlights();
    res.json({ success: true, message: 'Flights refreshed', count: cachedFlights.length });
});

app.get('/api/health', (req, res) => {
    res.json({ 
        status: 'ok', 
        flightsCount: cachedFlights.length,
        chatUsers: users.length,
        messagesCount: messages.length,
        pythonRunning: fleetProcess !== null && !fleetProcess.killed
    });
});

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

// ==================== START SERVER ====================
setInterval(fetchFlights, 30000);
fetchFlights();

server.listen(PORT, () => {
    console.log(`
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║   ✈️  BUDDHA AIR FLIGHT TRACKER + REAL-TIME CHAT            ║
    ║                                                              ║
    ║   🌐 http://localhost:${PORT}                                ║
    ║                                                              ║
    ║   🔑 TEST ACCOUNTS:                                          ║
    ║      Admin:      admin / 1221                               ║
    ║      Test User:  testuser / 1234                            ║
    ║      Technician: technician / tech123                       ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    `);
});