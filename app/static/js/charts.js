/**
 * Initialize a VO2 Max trend chart
 * @param {string} canvasId - The ID of the canvas element
 * @param {string} dataUrl - The URL to fetch chart data from
 */
function initVO2Chart(canvasId, dataUrl) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    fetch(dataUrl)
        .then(response => response.json())
        .then(data => {
            if (data.labels.length === 0) {
                // No data - show message
                const ctx = canvas.getContext('2d');
                ctx.font = '16px Arial';
                ctx.fillStyle = '#6c757d';
                ctx.textAlign = 'center';
                ctx.fillText('No VO2 max data available', canvas.width / 2, canvas.height / 2);
                return;
            }

            new Chart(canvas, {
                type: 'line',
                data: data,
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {
                        legend: {
                            display: false
                        },
                        tooltip: {
                            mode: 'index',
                            intersect: false,
                            callbacks: {
                                label: function(context) {
                                    return 'VO2 Max: ' + context.parsed.y.toFixed(1) + ' ml/kg/min';
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            display: true,
                            title: {
                                display: true,
                                text: 'Date'
                            },
                            ticks: {
                                maxRotation: 45,
                                minRotation: 45
                            }
                        },
                        y: {
                            display: true,
                            title: {
                                display: true,
                                text: 'VO2 Max (ml/kg/min)'
                            },
                            suggestedMin: function(context) {
                                const min = Math.min(...context.chart.data.datasets[0].data);
                                return Math.floor(min - 5);
                            },
                            suggestedMax: function(context) {
                                const max = Math.max(...context.chart.data.datasets[0].data);
                                return Math.ceil(max + 5);
                            }
                        }
                    },
                    interaction: {
                        mode: 'nearest',
                        axis: 'x',
                        intersect: false
                    },
                    elements: {
                        point: {
                            radius: 4,
                            hoverRadius: 6
                        },
                        line: {
                            borderWidth: 2
                        }
                    }
                }
            });
        })
        .catch(error => {
            console.error('Error loading chart data:', error);
            const ctx = canvas.getContext('2d');
            ctx.font = '16px Arial';
            ctx.fillStyle = '#dc3545';
            ctx.textAlign = 'center';
            ctx.fillText('Error loading chart data', canvas.width / 2, canvas.height / 2);
        });
}
