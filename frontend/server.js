'use strict';

const express = require('express');
const path = require('path');

const app = express();
const PORT = parseInt(process.env.PORT || '3000', 10);
const API_URL = process.env.API_URL || 'http://api:8000';

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

app.get('/health', (_req, res) => {
  res.json({ status: 'healthy' });
});

app.post('/api/jobs', async (req, res) => {
  try {
    const response = await fetch(`${API_URL}/jobs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err) {
    console.error('Error proxying POST /api/jobs:', err.message);
    res.status(502).json({ error: 'API unavailable' });
  }
});

app.get('/api/jobs/:id', async (req, res) => {
  try {
    const response = await fetch(`${API_URL}/jobs/${req.params.id}`);
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err) {
    console.error('Error proxying GET /api/jobs/:id:', err.message);
    res.status(502).json({ error: 'API unavailable' });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Frontend server listening on port ${PORT}`);
});
