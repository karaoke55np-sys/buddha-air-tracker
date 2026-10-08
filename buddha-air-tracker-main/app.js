// ==================== STATE MANAGEMENT ====================
let flights = [];
let previousFlights = [];
let notifications = [];
let currentFilter = 'all';
let searchTerm = '';
let currentView = 'tile';
let autoPopupEnabled = true;
let autoPopupDuration = 5000;
let backgroundNotifications = false;
let refreshInterval = null;
let flightHistory = [];
let currentDate = new Date().toISOString().split('T')[0];

// Flight status categories
let airborneFlights = [];
let onAirFlights = [];
let landingFlights = [];
let landedFlights = [];

// ==================== DOM ELEMENTS ====================
const flightsContainer = document.getElementById('flightsContainer');
const notifDropdown = document.getElementById('notifDropdown');
const notifList = document.getElementById('notifList');
const notifBadge = document.getElementById('notifBadge');
const searchInput = document.getElementById('searchInput');
const lastUpdatedSpan = document.getElementById('lastUpdated');
const settingsPanel = document.getElementById('settingsPanel');
const navDashboard = document.getElementById('navDashboard');
const navSettings = document.getElementById('navSettings');
const historyPanel = document.getElementById('historyPanel');
const toastContainer = document.getElementById('toastContainer');
const manualRefreshBtn = document.getElementById('manualRefresh');

// ==================== UTILITY FUNCTIONS ====================

function formatTimeAgo(date) {
    const seconds = Math.floor((new Date() - new Date(date)) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

function updateLiveClock() {
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-GB', {
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
    const clockElement = document.getElementById('liveClock');
    if (clockElement) clockElement.innerText = timeStr;
}
setInterval(updateLiveClock, 1000);
updateLiveClock();

// Show toast notification with duration control
function showToast(type, title, message, duration = autoPopupDuration) {
    if (!autoPopupEnabled && type !== 'info') return;
    
    const toast = document.createElement('div');
    toast.className = `toast-notification ${type}`;
    
    let icon = 'fa-bell';
    if (type === 'boarding') icon = 'fa-door-open';
    else if (type === 'revised') icon = 'fa-clock';
    else if (type === 'added') icon = 'fa-plus-circle';
    else if (type === 'cancelled') icon = 'fa-times-circle';
    else if (type === 'airborne') icon = 'fa-plane-up';
    else if (type === 'landing') icon = 'fa-plane-arrival';
    else if (type === 'landed') icon = 'fa-check-circle';
    else if (type === 'radar') icon = 'fa-radar';
    
    toast.style.borderLeftColor = 
        type === 'boarding' ? '#f59e0b' : 
        type === 'revised' ? '#a78bfa' : 
        type === 'airborne' ? '#3b82f6' : 
        type === 'landing' ? '#f59e0b' : 
        type === 'landed' ? '#10b981' : '#3b82f6';
    
    toast.innerHTML = `
        <div style="display: flex; gap: 12px; align-items: center; flex: 1;">
            <i class="fas ${icon}" style="font-size: 18px;"></i>
            <div>
                <div style="font-weight: 600; font-size: 0.9rem;">${title}</div>
                <div style="font-size: 0.8rem; color: var(--text-secondary);">${message}</div>
            </div>
        </div>
        <div class="toast-close" onclick="this.parentElement.remove()" style="cursor: pointer;">&times;</div>
    `;
    
    if (toastContainer) toastContainer.appendChild(toast);
    setTimeout(() => {
        if (toast.parentElement) toast.remove();
    }, duration);
}

// Save flight to history
function saveToHistory(flight, status) {
    const historyEntry = {
        id: flight.id,
        flightNumber: flight.flightNumber,
        destination: flight.destination,
        time: flight.time,
        date: new Date().toISOString(),
        status: status,
        departureTime: new Date().toISOString(),
        aircraft: flight.flightRadarData?.aircraft || 'N/A',
        registration: flight.flightRadarData?.registration || 'N/A'
    };
    
    flightHistory.unshift(historyEntry);
    
    // Keep last 500 entries
    if (flightHistory.length > 500) flightHistory.pop();
    
    // Save to localStorage
    localStorage.setItem('flightHistory', JSON.stringify(flightHistory));
    
    updateHistoryPanel();
}

// Load history from localStorage
function loadHistory() {
    const saved = localStorage.getItem('flightHistory');
    if (saved) {
        flightHistory = JSON.parse(saved);
    }
    updateHistoryPanel();
}

// Update history panel
function updateHistoryPanel() {
    const historyLog = document.getElementById('historyLog');
    if (!historyLog) return;
    
    const dateFilter = document.getElementById('historyDateFilter')?.value || currentDate;
    
    const filtered = flightHistory.filter(h => h.date.split('T')[0] === dateFilter);
    
    if (filtered.length === 0) {
        historyLog.innerHTML = `
            <div style="text-align: center; padding: 40px; grid-column: 1/-1;">
                <i class="fas fa-history"></i>
                <p>No flight history for ${dateFilter}</p>
            </div>
        `;
        return;
    }
    
    historyLog.innerHTML = filtered.map(entry => `
        <div class="history-entry" onclick="showFlightDetails('${entry.id}')">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <strong>${entry.flightNumber}</strong>
                <span class="status-badge ${entry.status.toLowerCase()}">${entry.status}</span>
            </div>
            <div style="font-size: 0.85rem;">${entry.destination}</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">
                <i class="far fa-clock"></i> ${new Date(entry.departureTime).toLocaleTimeString()}
                ${entry.aircraft !== 'N/A' ? `<br><i class="fas fa-plane"></i> ${entry.aircraft}` : ''}
            </div>
        </div>
    `).join('');
}

// Show flight details modal
window.showFlightDetails = function(flightId) {
    const flight = flightHistory.find(f => f.id === flightId);
    if (!flight) return;
    
    const modal = document.createElement('div');
    modal.className = 'modal-overlay';
    modal.innerHTML = `
        <div class="modal-content">
            <div class="modal-header">
                <h3>Flight ${flight.flightNumber} Details</h3>
                <button onclick="this.closest('.modal-overlay').remove()">&times;</button>
            </div>
            <div class="modal-body">
                <div><strong>Destination:</strong> ${flight.destination}</div>
                <div><strong>Scheduled Time:</strong> ${flight.time}</div>
                <div><strong>Departure:</strong> ${new Date(flight.departureTime).toLocaleString()}</div>
                <div><strong>Status:</strong> ${flight.status}</div>
                <div><strong>Aircraft:</strong> ${flight.aircraft}</div>
                <div><strong>Registration:</strong> ${flight.registration}</div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
};

// ==================== NOTIFICATION FUNCTIONS ====================

function addNotification(change) {
    const notification = {
        id: Date.now() + Math.random(),
        type: change.type,
        message: change.message,
        time: change.time,
        read: false
    };
    
    notifications.unshift(notification);
    if (notifications.length > 50) notifications.pop();
    
    updateNotificationCenter();
    
    let title = 'Buddha Air Update';
    if (change.type === 'boarding') title = '🟡 Now Boarding';
    else if (change.type === 'revised') title = '🔄 Flight Revised';
    else if (change.type === 'airborne') title = '✈️ Airborne';
    else if (change.type === 'landing') title = '🛬 Landing Soon';
    else if (change.type === 'landed') title = '✅ Flight Landed';
    
    showToast(change.type, title, change.message);
    
    // Background notification - FIXED
    if (backgroundNotifications && Notification.permission === 'granted') {
        const notificationOptions = {
            body: change.message,
            icon: 'https://buddhaair.com/favicon.ico',
            tag: change.type,
            requireInteraction: true,
            silent: false
        };
        
        new Notification(title, notificationOptions);
    }
}

function updateNotificationCenter() {
    const unreadCount = notifications.filter(n => !n.read).length;
    if (notifBadge) {
        notifBadge.textContent = unreadCount;
        notifBadge.style.display = unreadCount > 0 ? 'flex' : 'none';
    }
    
    if (!notifList) return;
    
    if (notifications.length === 0) {
        notifList.innerHTML = `
            <div style="text-align: center; padding: 40px; color: var(--text-muted);">
                <i class="fas fa-bell-slash" style="font-size: 2rem;"></i>
                <p>No notifications</p>
            </div>
        `;
        return;
    }
    
    notifList.innerHTML = notifications.slice(0, 20).map(notif => {
        let icon = 'fa-bell';
        if (notif.type === 'boarding') icon = 'fa-door-open';
        else if (notif.type === 'revised') icon = 'fa-clock';
        else if (notif.type === 'airborne') icon = 'fa-plane-up';
        else if (notif.type === 'landing') icon = 'fa-plane-arrival';
        else if (notif.type === 'landed') icon = 'fa-check-circle';
        
        return `
            <div class="notification-item ${notif.type} ${notif.read ? '' : 'unread'}" onclick="window.markNotificationRead('${notif.id}')">
                <i class="fas ${icon}"></i>
                <div class="notification-content">
                    <div class="notification-title">${notif.message}</div>
                    <div class="notification-time">${formatTimeAgo(notif.time)}</div>
                </div>
            </div>
        `;
    }).join('');
}

window.markNotificationRead = function(id) {
    const notification = notifications.find(n => n.id == id);
    if (notification) {
        notification.read = true;
        updateNotificationCenter();
    }
};

function markAllRead() {
    notifications.forEach(n => n.read = true);
    updateNotificationCenter();
}

// ==================== FLIGHT STATUS TRACKING ====================

function updateFlightStatusCategories() {
    const now = new Date();
    const currentHour = now.getHours();
    const currentMinute = now.getMinutes();
    
    airborneFlights = [];
    onAirFlights = [];
    landingFlights = [];
    landedFlights = [];
    
    flights.forEach(flight => {
        const [hours, minutes] = flight.time.split(':').map(Number);
        const flightTime = hours * 60 + minutes;
        const currentTime = currentHour * 60 + currentMinute;
        const timeDiff = flightTime - currentTime;
        
        // Check radar data for real-time status
        if (flight.flightRadarData && flight.flightRadarData.aircraft !== 'Unknown') {
            const radar = flight.flightRadarData;
            
            if (radar.onGround === false && radar.altitude !== 'N/A') {
                if (parseInt(radar.altitude) > 5000) {
                    airborneFlights.push(flight);
                    onAirFlights.push(flight);
                    
                    // Check if landing soon (low altitude)
                    if (parseInt(radar.altitude) < 5000) {
                        landingFlights.push(flight);
                        if (!previousFlights.find(p => p.id === flight.id && 
                            p.flightRadarData?.altitude !== radar.altitude)) {
                            addNotification({
                                type: 'landing',
                                message: `🛬 ${flight.flightNumber} approaching ${flight.destination} at ${radar.altitude}`,
                                time: new Date()
                            });
                        }
                    }
                }
            } else if (radar.onGround === true) {
                landedFlights.push(flight);
                if (!previousFlights.find(p => p.id === flight.id && p.flightRadarData?.onGround !== true)) {
                    addNotification({
                        type: 'landed',
                        message: `✅ ${flight.flightNumber} has landed at ${flight.destination}`,
                        time: new Date()
                    });
                    saveToHistory(flight, 'Landed');
                }
            }
        }
    });
    
    updateStatusTabs();
}

function updateStatusTabs() {
    const airborneCount = document.getElementById('airborneCount');
    const onAirCount = document.getElementById('onAirCount');
    const landingCount = document.getElementById('landingCount');
    const landedCount = document.getElementById('landedCount');
    
    if (airborneCount) airborneCount.textContent = airborneFlights.length;
    if (onAirCount) onAirCount.textContent = onAirFlights.length;
    if (landingCount) landingCount.textContent = landingFlights.length;
    if (landedCount) landedCount.textContent = landedFlights.length;
}

// ==================== RADAR TRACKING WITH MAP ====================

async function trackFlightWithMap(flightNumber) {
    try {
        showToast('radar', 'Tracking Flight', `Fetching real-time data for ${flightNumber}...`, 3000);
        
        const response = await fetch(`/api/track/${encodeURIComponent(flightNumber)}`);
        const data = await response.json();
        
        if (data.success && data.tracking && data.tracking.aircraft) {
            const track = data.tracking;
            showFlightMapModal(flightNumber, track);
        } else {
            showToast('revised', 'No Tracking Data', `Unable to track ${flightNumber}`, 4000);
        }
    } catch (error) {
        console.error('Tracking error:', error);
        showToast('revised', 'Tracking Error', 'Could not fetch real-time data', 4000);
    }
}

function showFlightMapModal(flightNumber, trackData) {
    const existingModal = document.getElementById('trackingModal');
    if (existingModal) existingModal.remove();
    
    const modalHtml = `
        <div id="trackingModal" class="modal-overlay">
            <div class="modal-content" style="max-width: 600px;">
                <div class="modal-header">
                    <h3><i class="fas fa-radar"></i> Flight ${flightNumber} Live Tracking</h3>
                    <button onclick="this.closest('.modal-overlay').remove()">&times;</button>
                </div>
                <div class="modal-body">
                    <div style="background: linear-gradient(135deg, #0a1a2a, #0a0f1a); border-radius: 16px; padding: 20px; margin-bottom: 16px;">
                        <div style="display: grid; gap: 12px;">
                            <div><i class="fas fa-plane"></i> Aircraft: <strong>${trackData.aircraft || 'N/A'}</strong></div>
                            <div><i class="fas fa-id-card"></i> Registration: <strong>${trackData.registration || 'N/A'}</strong></div>
                            <div><i class="fas fa-arrow-up"></i> Altitude: <strong>${trackData.altitude || 'N/A'}</strong></div>
                            <div><i class="fas fa-tachometer-alt"></i> Speed: <strong>${trackData.speed || 'N/A'}</strong></div>
                            <div><i class="fas fa-compass"></i> Heading: <strong>${trackData.heading || 'N/A'}</strong></div>
                            <div><i class="fas fa-clock"></i> Est. Landing: <strong>${trackData.estimatedLanding || 'Calculating...'}</strong></div>
                            <div><i class="fas fa-circle"></i> Status: 
                                <strong style="color: ${trackData.onGround ? '#10b981' : '#f59e0b'}">
                                    ${trackData.onGround ? '🟢 On Ground' : '🟡 In Air'}
                                </strong>
                            </div>
                        </div>
                    </div>
                    
                    <div style="background: var(--glass-bg); border-radius: 16px; padding: 20px;">
                        <h4 style="margin-bottom: 12px;"><i class="fas fa-map-marked-alt"></i> Live Position</h4>
                        <div id="simpleMap" style="background: linear-gradient(135deg, #1a3a4a, #0a1a2a); border-radius: 12px; height: 200px; display: flex; align-items: center; justify-content: center; position: relative; overflow: hidden;">
                            <div style="text-align: center;">
                                <i class="fas fa-plane" style="font-size: 48px; color: var(--primary); animation: float 3s infinite;"></i>
                                <div style="margin-top: 12px;">
                                    ${trackData.lat && trackData.lon ? 
                                        `<div>📍 ${trackData.lat}, ${trackData.lon}</div>
                                         <div style="font-size: 0.8rem; margin-top: 8px;">
                                            <span class="status-badge ${trackData.onGround ? 'landed' : 'airborne'}">
                                                ${trackData.onGround ? 'ON GROUND' : 'IN FLIGHT'}
                                            </span>
                                         </div>` : 
                                        `<div>📡 Tracking signal acquired</div>
                                         <div style="font-size: 0.8rem; margin-top: 8px;">Waiting for position data...</div>`
                                    }
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
    
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    
    // Animate the plane icon
    const planeIcon = document.querySelector('#simpleMap i');
    if (planeIcon && !trackData.onGround) {
        setInterval(() => {
            if (planeIcon) {
                planeIcon.style.transform = `translateX(${Math.sin(Date.now() / 1000) * 10}px) translateY(${Math.cos(Date.now() / 1000) * 5}px)`;
            }
        }, 100);
    }
}

// ==================== DATA LOADING ====================

async function loadFlights() {
    try {
        const response = await fetch('/api/flights');
        const data = await response.json();
        
        if (data.success) {
            previousFlights = [...flights];
            flights = data.flights || [];
            
            detectChanges();
            updateFlightStatusCategories();
            updateStats();
            filterAndDisplay();
            updateLastUpdated();
        }
    } catch (error) {
        console.error('Error loading flights:', error);
    }
}

function detectChanges() {
    if (previousFlights.length === 0) return;
    
    const changes = [];
    
    // Check for removed flights (departed/landed)
    previousFlights.forEach(prev => {
        const exists = flights.some(f => f.id === prev.id);
        if (!exists) {
            changes.push({
                type: 'landed',
                message: `✅ ${prev.flightNumber} has departed/landed at ${prev.destination}`,
                time: new Date()
            });
            saveToHistory(prev, 'Departed');
        }
    });
    
    // Check for new flights
    flights.forEach(flight => {
        const exists = previousFlights.some(p => p.id === flight.id);
        if (!exists) {
            changes.push({
                type: 'added',
                message: `✈️ New flight: ${flight.flightNumber} to ${flight.destination}`,
                time: new Date()
            });
        }
    });
    
    // Check for status changes
    flights.forEach(flight => {
        const previous = previousFlights.find(p => p.id === flight.id);
        if (previous) {
            const currStatus = (flight.status || '').toLowerCase();
            const prevStatus = (previous.status || '').toLowerCase();
            const currRemarks = (flight.remarks || '').toLowerCase();
            
            if ((currRemarks.includes('boarding') || currStatus.includes('boarding')) && 
                !prevStatus.includes('boarding')) {
                changes.push({
                    type: 'boarding',
                    message: `🟡 NOW BOARDING: ${flight.flightNumber} to ${flight.destination}`,
                    time: new Date()
                });
            }
            
            if ((currStatus.includes('delay') || currStatus.includes('late')) && 
                !prevStatus.includes('delay')) {
                changes.push({
                    type: 'revised',
                    message: `🔄 REVISED: ${flight.flightNumber} to ${flight.destination} - ${flight.status}`,
                    time: new Date()
                });
            }
        }
    });
    
    changes.forEach(change => addNotification(change));
}

function updateStats() {
    const total = flights.length;
    const onTime = flights.filter(f => {
        const status = (f.status || '').toLowerCase();
        return (status.includes('on time') || status.includes('ontime')) && 
               !status.includes('delay') && 
               !(f.remarks || '').toLowerCase().includes('boarding');
    }).length;
    
    const boarding = flights.filter(f => {
        const remarks = (f.remarks || '').toLowerCase();
        const status = (f.status || '').toLowerCase();
        return remarks.includes('boarding') || status.includes('boarding');
    }).length;
    
    const revised = flights.filter(f => {
        const status = (f.status || '').toLowerCase();
        return status.includes('delay') || status.includes('late');
    }).length;
    
    const totalEl = document.getElementById('totalFlights');
    const onTimeEl = document.getElementById('onTimeCount');
    const boardingEl = document.getElementById('boardingCount');
    const revisedEl = document.getElementById('revisedCount');
    
    if (totalEl) totalEl.textContent = total;
    if (onTimeEl) onTimeEl.textContent = onTime;
    if (boardingEl) boardingEl.textContent = boarding;
    if (revisedEl) revisedEl.textContent = revised;
}

function updateLastUpdated() {
    const now = new Date();
    if (lastUpdatedSpan) lastUpdatedSpan.textContent = `updated ${now.toLocaleTimeString()}`;
}

// ==================== DISPLAY FUNCTIONS ====================

function filterAndDisplay() {
    let filtered = [...flights];
    
    if (currentFilter === 'on-time') {
        filtered = flights.filter(f => {
            const status = (f.status || '').toLowerCase();
            return (status.includes('on time') || status.includes('ontime')) && 
                   !status.includes('delay') && 
                   !(f.remarks || '').toLowerCase().includes('boarding');
        });
    } else if (currentFilter === 'boarding') {
        filtered = flights.filter(f => {
            const remarks = (f.remarks || '').toLowerCase();
            const status = (f.status || '').toLowerCase();
            return remarks.includes('boarding') || status.includes('boarding');
        });
    } else if (currentFilter === 'revised') {
        filtered = flights.filter(f => {
            const status = (f.status || '').toLowerCase();
            return status.includes('delay') || status.includes('late');
        });
    } else if (currentFilter === 'airborne') {
        filtered = airborneFlights;
    } else if (currentFilter === 'onair') {
        filtered = onAirFlights;
    } else if (currentFilter === 'landing') {
        filtered = landingFlights;
    } else if (currentFilter === 'landed') {
        filtered = landedFlights;
    }
    
    if (searchTerm) {
        const term = searchTerm.toLowerCase();
        filtered = filtered.filter(f => 
            (f.flightNumber || '').toLowerCase().includes(term) ||
            (f.destination || '').toLowerCase().includes(term)
        );
    }
    
    filtered.sort((a, b) => a.time.localeCompare(b.time));
    displayFlights(filtered);
}

function displayFlights(flightsToShow) {
    if (!flightsContainer) return;
    
    if (flightsToShow.length === 0) {
        flightsContainer.innerHTML = `
            <div class="empty-state">
                <i class="fas fa-plane-slash"></i>
                <h3>No flights found</h3>
                <p>Try adjusting your filters or search criteria</p>
            </div>
        `;
        return;
    }
    
    flightsContainer.innerHTML = flightsToShow.map(flight => {
        const status = (flight.status || '').toLowerCase();
        const remarks = (flight.remarks || '').toLowerCase();
        const isBoarding = remarks.includes('boarding') || status.includes('boarding');
        const isRevised = status.includes('delay') || status.includes('late');
        
        let statusClass = 'on-time';
        let statusText = flight.status || 'On Time';
        if (isBoarding) {
            statusClass = 'boarding';
            statusText = 'BOARDING';
        } else if (isRevised) {
            statusClass = 'revised';
            statusText = 'Revised';
        }
        
        const hasRadar = flight.flightRadarData && flight.flightRadarData.aircraft;
        const isAirborne = airborneFlights.some(f => f.id === flight.id);
        const isLanding = landingFlights.some(f => f.id === flight.id);
        
        return `
            <div class="flight-card ${isAirborne ? 'airborne' : ''} ${isLanding ? 'landing' : ''}">
                <div class="flight-header">
                    <div>
                        <div class="flight-number">${flight.flightNumber || 'N/A'}</div>
                        <div class="flight-route">
                            <i class="fas fa-map-marker-alt"></i> KTM → ${flight.destination || 'TBD'}
                        </div>
                    </div>
                    <div class="gate-badge">Gate ${flight.gate || 'TBD'}</div>
                </div>
                
                <div class="flight-body">
                    <div class="flight-time">
                        <i class="far fa-clock"></i> ${flight.time || '--:--'}
                    </div>
                    <div class="flight-status ${statusClass}">${statusText}</div>
                </div>
                
                ${isAirborne ? `
                    <div class="radar-data airborne-tag">
                        <i class="fas fa-plane-up"></i> AIRBORNE
                        ${flight.flightRadarData?.altitude ? ` · ${flight.flightRadarData.altitude}` : ''}
                    </div>
                ` : isLanding ? `
                    <div class="radar-data landing-tag">
                        <i class="fas fa-plane-arrival"></i> APPROACHING
                    </div>
                ` : hasRadar ? `
                    <div class="radar-data">
                        <span><i class="fas fa-plane"></i> ${flight.flightRadarData.aircraft}</span>
                        <span><i class="fas fa-arrow-up"></i> ${flight.flightRadarData.altitude}</span>
                        <span><i class="fas fa-tachometer-alt"></i> ${flight.flightRadarData.speed}</span>
                    </div>
                ` : ''}
                
                <div class="flight-footer">
                    <div class="flight-destination">
                        <i class="fas fa-location-dot"></i> ${flight.destination}
                    </div>
                    <button class="track-btn" onclick="trackFlightWithMap('${flight.flightNumber}')">
                        <i class="fas fa-radar"></i> Track Live
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

// ==================== EVENT LISTENERS ====================

function setupEventListeners() {
    // Search input
    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            searchTerm = e.target.value;
            filterAndDisplay();
        });
    }
    
    // Filter tabs
    document.querySelectorAll('.filter-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentFilter = tab.dataset.filter;
            filterAndDisplay();
        });
    });
    
    // Status category tabs
    document.querySelectorAll('.status-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.status-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentFilter = tab.dataset.filter;
            filterAndDisplay();
        });
    });
    
    // View buttons
    document.querySelectorAll('.view-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentView = btn.dataset.view;
            flightsContainer.className = `flights-grid ${currentView === 'list' ? 'list-view' : ''}`;
            filterAndDisplay();
        });
    });
    
    // Stat cards click
    document.querySelectorAll('.stat-card').forEach(card => {
        card.addEventListener('click', () => {
            const filter = card.dataset.filter;
            document.querySelectorAll('.filter-tab').forEach(tab => {
                tab.classList.toggle('active', tab.dataset.filter === filter);
            });
            currentFilter = filter;
            filterAndDisplay();
        });
    });
    
    // Manual refresh
    if (manualRefreshBtn) {
        manualRefreshBtn.addEventListener('click', () => {
            const icon = manualRefreshBtn.querySelector('i');
            if (icon) {
                icon.style.animation = 'spin 0.5s linear';
                setTimeout(() => icon.style.animation = '', 500);
            }
            loadFlights();
            showToast('info', 'Refresh', 'Flight data updated', 2000);
        });
    }
    
    // Notification bell
    const notifBtn = document.getElementById('notifBtn');
    if (notifBtn) {
        notifBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            notifDropdown.classList.toggle('show');
        });
    }
    
    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
        const notifBtn = document.getElementById('notifBtn');
        if (notifDropdown && notifBtn) {
            if (!notifBtn.contains(e.target) && !notifDropdown.contains(e.target)) {
                notifDropdown.classList.remove('show');
            }
        }
    });
    
    // Navigation
    if (navDashboard) {
        navDashboard.addEventListener('click', (e) => {
            e.preventDefault();
            navDashboard.classList.add('active');
            if (navSettings) navSettings.classList.remove('active');
            if (settingsPanel) settingsPanel.style.display = 'none';
            if (historyPanel) historyPanel.style.display = 'none';
        });
    }
    
    if (navSettings) {
        navSettings.addEventListener('click', (e) => {
            e.preventDefault();
            navSettings.classList.add('active');
            if (navDashboard) navDashboard.classList.remove('active');
            if (settingsPanel) settingsPanel.style.display = 'block';
            if (historyPanel) historyPanel.style.display = 'none';
        });
    }
    
    // Auto Popup Duration Slider
    const durationSlider = document.getElementById('popupDuration');
    const durationValue = document.getElementById('durationValue');
    if (durationSlider && durationValue) {
        durationSlider.addEventListener('input', (e) => {
            autoPopupDuration = parseInt(e.target.value);
            durationValue.textContent = `${autoPopupDuration / 1000}s`;
            localStorage.setItem('popupDuration', autoPopupDuration);
        });
    }
    
    // Load saved duration
    const savedDuration = localStorage.getItem('popupDuration');
    if (savedDuration) autoPopupDuration = parseInt(savedDuration);
    
    // Auto Popup Toggle
    const autoPopupToggle = document.getElementById('autoPopupToggle');
    if (autoPopupToggle) {
        autoPopupToggle.addEventListener('click', () => {
            autoPopupEnabled = !autoPopupEnabled;
            autoPopupToggle.classList.toggle('active', autoPopupEnabled);
            localStorage.setItem('autoPopupEnabled', autoPopupEnabled);
            showToast('info', 'Settings', `Auto popup ${autoPopupEnabled ? 'enabled' : 'disabled'}`, 2000);
        });
        autoPopupToggle.classList.add(autoPopupEnabled ? 'active' : '');
    }
    
    // Background Notifications Toggle - FIXED
    const backgroundToggle = document.getElementById('backgroundNotifToggle');
    if (backgroundToggle) {
        backgroundToggle.addEventListener('click', async () => {
            backgroundNotifications = !backgroundNotifications;
            backgroundToggle.classList.toggle('active', backgroundNotifications);
            
            if (backgroundNotifications && Notification.permission !== 'granted') {
                const permission = await Notification.requestPermission();
                if (permission === 'granted') {
                    showToast('info', 'Notifications', 'Background notifications enabled - You will receive alerts even when browser is minimized', 4000);
                    // Test notification
                    new Notification('Buddha Air', {
                        body: 'Background notifications are now active!',
                        icon: 'https://buddhaair.com/favicon.ico'
                    });
                } else {
                    backgroundNotifications = false;
                    backgroundToggle.classList.remove('active');
                    showToast('revised', 'Permission Denied', 'Please allow notifications in browser settings', 3000);
                }
            } else if (backgroundNotifications) {
                showToast('info', 'Notifications', 'Background notifications enabled', 3000);
                // Test notification
                new Notification('Buddha Air', {
                    body: 'You will now receive flight updates even when browser is minimized',
                    icon: 'https://buddhaair.com/favicon.ico'
                });
            }
        });
        
        // Load saved setting
        const savedBg = localStorage.getItem('backgroundNotifications');
        if (savedBg === 'true') {
            backgroundNotifications = true;
            backgroundToggle.classList.add('active');
        }
    }
    
    // Save settings to localStorage
    const saveSettings = () => {
        localStorage.setItem('backgroundNotifications', backgroundNotifications);
        localStorage.setItem('autoPopupEnabled', autoPopupEnabled);
        localStorage.setItem('popupDuration', autoPopupDuration);
    };
    
    window.addEventListener('beforeunload', saveSettings);
    
    // History Date Filter
    const historyDateFilter = document.getElementById('historyDateFilter');
    if (historyDateFilter) {
        historyDateFilter.value = currentDate;
        historyDateFilter.addEventListener('change', (e) => {
            currentDate = e.target.value;
            updateHistoryPanel();
        });
    }
    
    // Mark all read button
    const markAllReadBtn = document.getElementById('markAllRead');
    if (markAllReadBtn) {
        markAllReadBtn.addEventListener('click', markAllRead);
    }
}

// ==================== INITIALIZATION ====================

function init() {
    // Load saved settings
    const savedBg = localStorage.getItem('backgroundNotifications');
    if (savedBg === 'true') backgroundNotifications = true;
    
    const savedAuto = localStorage.getItem('autoPopupEnabled');
    if (savedAuto !== null) autoPopupEnabled = savedAuto === 'true';
    
    const savedDuration = localStorage.getItem('popupDuration');
    if (savedDuration) autoPopupDuration = parseInt(savedDuration);
    
    // Load history
    loadHistory();
    
    // Request notification permission on load
    if (Notification.permission === 'default') {
        Notification.requestPermission();
    }
    
    // Load initial data
    loadFlights();
    
    // Setup event listeners
    setupEventListeners();
    
    // Auto refresh every 30 seconds
    setInterval(loadFlights, 30000);
    
    // Welcome notification
    setTimeout(() => {
        showToast('radar', 'Buddha Air Radar', 'Live flight tracking with FlightRadar24 active', 5000);
    }, 2000);
}

// Export functions for global access
window.trackFlightWithMap = trackFlightWithMap;
window.markNotificationRead = window.markNotificationRead;
window.loadFlights = loadFlights;
window.showFlightDetails = showFlightDetails;

// Start the app
document.addEventListener('DOMContentLoaded', init);