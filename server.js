const express = require('express');
const cors = require('cors');
const axios = require('axios');
const cheerio = require('cheerio');
const path = require('path');
const http = require('http');
const socketIo = require('socket.io');
const fs = require('fs');

const app = express();
const server = http.createServer(app);
const io = socketIo(server, {
    cors: { origin: "*" },
    transports: ['websocket', 'polling']
});

const PORT = process.env.PORT || 3000;
const DATA_FILE = './chat-data.json';

app.use(cors());
app.use(express.json());
app.use(express.static(__dirname));

// ==================== CHAT DATA ====================
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
        }
        if (!users.some(u => u.username === 'admin')) {
            users.push(...defaultUsers);
            saveData();
        }
        console.log(`📁 Loaded: ${users.length} users, ${messages.length} messages`);
    } catch (e) { 
        users = [...defaultUsers];
        saveData();
    }
}

function saveData() {
    fs.writeFileSync(DATA_FILE, JSON.stringify({ users, messages }, null, 2));
}

loadData();

// ==================== SOCKET.IO CHAT ====================
io.on('connection', (socket) => {
    console.log('🔌 User connected:', socket.id);
    
    socket.on('user-login', (data) => {
        const user = users.find(u => u.username === data.username);
        if (!user) {
            socket.emit('login-error', 'User not found');
        } else if (user.password !== data.password) {
            socket.emit('login-error', 'Wrong password');
        } else {
            socket.emit('login-success', {
                id: user.id,
                username: user.username,
                displayName: user.displayName,
                isAdmin: user.isAdmin
            });
            socket.broadcast.emit('user-online', user.displayName);
        }
    });
    
    socket.on('user-register', (data) => {
        if (users.find(u => u.username === data.username)) {
            socket.emit('register-error', 'Username exists');
            return;
        }
        const newUser = {
            id: Date.now(),
            username: data.username,
            password: data.password,
            displayName: data.displayName || data.username,
            isAdmin: false,
            createdAt: new Date().toISOString()
        };
        users.push(newUser);
        saveData();
        socket.emit('register-success', 'Registration successful!');
    });
    
    socket.on('forgot-password', (data) => {
        const user = users.find(u => u.username === data.username);
        if (user) {
            socket.emit('reset-allowed', { userId: user.id, username: user.username, displayName: user.displayName });
        } else {
            socket.emit('reset-error', 'User not found');
        }
    });
    
    socket.on('reset-password-direct', (data) => {
        const user = users.find(u => u.id === data.userId);
        if (user) {
            user.password = data.newPassword;
            saveData();
            socket.emit('password-reset-success', 'Password reset successful!');
        }
    });
    
    socket.on('get-messages', () => {
        socket.emit('messages-history', messages);
    });
    
    socket.on('send-message', (data) => {
        const newMessage = {
            id: Date.now(),
            text: data.text,
            userName: data.userName,
            userId: data.userId,
            timestamp: new Date().toISOString(),
            groupId: data.groupId || 'general'
        };
        if (data.replyTo) {
            newMessage.replyTo = data.replyTo;
        }
        messages.push(newMessage);
        if (messages.length > 500) messages.shift();
        saveData();
        io.emit('new-message', newMessage);
    });
    
    socket.on('edit-message', (data) => {
        const user = users.find(u => u.id === data.userId);
        const message = messages.find(m => m.id === data.messageId);
        if (message && (message.userId === data.userId || user?.isAdmin)) {
            message.text = data.newText;
            message.edited = true;
            saveData();
            io.emit('message-edited', { messageId: data.messageId, newText: data.newText });
        }
    });
    
    socket.on('delete-message', (data) => {
        const user = users.find(u => u.id === data.userId);
        const index = messages.findIndex(m => m.id === data.messageId);
        if (index !== -1 && (messages[index].userId === data.userId || user?.isAdmin)) {
            messages.splice(index, 1);
            saveData();
            io.emit('message-deleted', { messageId: data.messageId });
        }
    });
    
    socket.on('typing', (data) => {
        socket.broadcast.emit('user-typing', { userName: data.userName, isTyping: data.isTyping });
    });
    
    socket.on('disconnect', () => {
        console.log('🔌 User disconnected:', socket.id);
    });
});

// ==================== FLIGHT DATA SCRAPER (FIXED - REVISED DETECTION) ====================
let cachedFlights = [];

async function fetchFlights() {
    try {
        console.log('\n✈️ Fetching flight data from Buddha Air desk...');
        const response = await axios.get('https://desk.buddhaair.com/station/ktm', {
            headers: { 
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            },
            timeout: 15000
        });
        
        const $ = cheerio.load(response.data);
        const flights = [];
        
        console.log('📊 Scanning for flights including REVISED status...');
        
        $('table tbody tr, table tr').each((index, element) => {
            const columns = $(element).find('td');
            if (columns.length >= 5) {
                const time = $(columns[0]).text().trim();
                const destination = $(columns[1]).text().trim();
                const flightNumberRaw = $(columns[2]).text().trim();
                let statusText = $(columns[3]).text().trim();
                const remarks = $(columns[4]).text().trim();
                
                if (time && destination && flightNumberRaw) {
                    // Check for yellow background (boarding)
                    const bgColor = $(element).attr('bgcolor') || '';
                    const style = $(element).attr('style') || '';
                    const isYellow = bgColor === '#FFFF00' || bgColor === '#FFD700' || 
                                   style.includes('#FFFF00') || style.includes('#FFD700');
                    
                    const remarksLower = remarks.toLowerCase();
                    const statusLower = statusText.toLowerCase();
                    
                    // Determine status with clear priority
                    let finalStatus = 'On Time';
                    
                    // Priority 1: BOARDING (yellow row or boarding text)
                    if (isYellow || remarksLower.includes('boarding') || statusLower.includes('boarding')) {
                        finalStatus = 'Boarding';
                        console.log(`   🟡 BOARDING: ${flightNumberRaw} to ${destination}`);
                    }
                    // Priority 2: REVISED (exact match or in remarks)
                    else if (statusText === 'Revised' || statusLower === 'revised' || remarksLower.includes('revised')) {
                        finalStatus = 'Revised';
                        console.log(`   🔄 REVISED: ${flightNumberRaw} to ${destination} - Status: "${statusText}" Remarks: "${remarks}"`);
                    }
                    // Priority 3: DELAYED
                    else if (statusText === 'Delayed' || statusLower === 'delayed' || remarksLower.includes('delay')) {
                        finalStatus = 'Delayed';
                        console.log(`   ⏰ DELAYED: ${flightNumberRaw} to ${destination}`);
                    }
                    // Priority 4: ON TIME (default)
                    else {
                        finalStatus = 'On Time';
                    }
                    
                    // Extract gate
                    let gate = 'TBD';
                    const gateMatch = remarks.match(/GATE\s*(\d+)/i);
                    if (gateMatch) gate = gateMatch[1];
                    
                    flights.push({
                        id: `BUD${flightNumberRaw}`,
                        flightNumber: `BUD ${flightNumberRaw}`,
                        time: time,
                        destination: destination,
                        destinationCode: destination.substring(0, 3).toUpperCase(),
                        route: `KTM → ${destination}`,
                        status: finalStatus,
                        remarks: remarks || (finalStatus === 'Boarding' ? 'NOW BOARDING' : 'Scheduled'),
                        gate: gate,
                        lastUpdated: new Date().toISOString()
                    });
                }
            }
        });
        
        // Sort: Boarding first, then Revised, then by time
        flights.sort((a, b) => {
            const priority = { 'Boarding': 1, 'Revised': 2, 'Delayed': 3, 'On Time': 4 };
            if (priority[a.status] !== priority[b.status]) {
                return priority[a.status] - priority[b.status];
            }
            return a.time.localeCompare(b.time);
        });
        
        cachedFlights = flights;
        
        // Log summary
        const boarding = flights.filter(f => f.status === 'Boarding').length;
        const revised = flights.filter(f => f.status === 'Revised').length;
        const delayed = flights.filter(f => f.status === 'Delayed').length;
        const onTime = flights.filter(f => f.status === 'On Time').length;
        
        console.log(`\n📊 FLIGHT STATUS SUMMARY:`);
        console.log(`   ✈️ Total: ${flights.length}`);
        console.log(`   🟡 Boarding: ${boarding}`);
        console.log(`   🔄 Revised: ${revised}`);
        console.log(`   ⏰ Delayed: ${delayed}`);
        console.log(`   ✅ On Time: ${onTime}`);
        
        if (revised > 0) {
            console.log(`\n📋 REVISED FLIGHTS DETAILS:`);
            flights.filter(f => f.status === 'Revised').forEach(f => {
                console.log(`   🔄 ${f.flightNumber} to ${f.destination} at ${f.time}`);
            });
        }
        
    } catch (error) {
        console.error('❌ Error fetching flights:', error.message);
    }
}

// API endpoint
app.get('/api/flights', (req, res) => {
    const boarding = cachedFlights.filter(f => f.status === 'Boarding').length;
    const revised = cachedFlights.filter(f => f.status === 'Revised').length;
    const delayed = cachedFlights.filter(f => f.status === 'Delayed').length;
    const onTime = cachedFlights.filter(f => f.status === 'On Time').length;
    
    console.log(`\n📤 API Response: Total=${cachedFlights.length}, Boarding=${boarding}, Revised=${revised}, Delayed=${delayed}, On Time=${onTime}`);
    
    res.json({
        success: true,
        flights: cachedFlights,
        stats: {
            total: cachedFlights.length,
            boarding: boarding,
            revised: revised,
            delayed: delayed,
            onTime: onTime
        },
        lastUpdated: new Date().toISOString()
    });
});

// Manual refresh endpoint
app.post('/api/refresh', async (req, res) => {
    await fetchFlights();
    res.json({ success: true, message: 'Flights refreshed', count: cachedFlights.length });
});

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

// Start fetching
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
    ║   📊 STATUS DETECTION (FIXED):                               ║
    ║      🟡 Boarding - Yellow rows or "Boarding" text           ║
    ║      🔄 Revised - Status="Revised" or "revised" in remarks  ║
    ║      ⏰ Delayed - Status="Delayed" or "delay" in remarks    ║
    ║      ✅ On Time - Default                                    ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    `);
});