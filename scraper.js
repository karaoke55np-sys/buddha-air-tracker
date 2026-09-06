async function scrapeBuddhaAirFlights() {
    try {
        console.log('✈️ Fetching flight data from Buddha Air...');
        const { data } = await axios.get('https://desk.buddhaair.com/station/ktm', {
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout: 10000
        });
        
        const $ = cheerio.load(data);
        const flights = [];

        $('table tbody tr').each((index, element) => {
            const columns = $(element).find('td');
            
            if (columns.length >= 5) {
                const time = $(columns[0]).text().trim();
                const destination = $(columns[1]).text().trim();
                const flightNumber = $(columns[2]).text().trim();
                const status = $(columns[3]).text().trim();
                const remarks = $(columns[4]).text().trim();

                if (time && destination && flightNumber) {
                    flights.push({
                        id: flightNumber,
                        flight: `BUD ${flightNumber}`,
                        flightNumber: `BUD ${flightNumber}`,
                        time: time,
                        destination: destination,
                        destinationCode: getDestinationCode(destination),
                        route: `KTM → ${destination}`,
                        status: status || 'On Time',
                        remarks: remarks,
                        gate: extractGateInfo(remarks) || 'TBD',
                        lastUpdated: new Date().toISOString(),
                        flightRadarData: null
                    });
                }
            }
        });

        console.log(`✅ Scraped ${flights.length} flights from Buddha Air desk`);
        
        // Fetch FlightRadar24 data for each flight (limited to avoid rate limiting)
        console.log('📡 Fetching FlightRadar24 data...');
        for (let i = 0; i < Math.min(flights.length, 10); i++) {
            const flight = flights[i];
            const radarData = await fetchFlightRadarData(flight.flightNumber);
            if (radarData) {
                flight.flightRadarData = radarData;
            }
            // Small delay to avoid rate limiting
            await new Promise(resolve => setTimeout(resolve, 500));
        }
        
        return flights;
        
    } catch (error) {
        console.error('❌ Error scraping Buddha Air:', error.message);
        return [];
    }
}