'use strict';
if (!process.env.APPDYNAMICS_CONTROLLER_HOST_NAME) return;
try {
  require('appdynamics').profile({
    controllerHostName:   process.env.APPDYNAMICS_CONTROLLER_HOST_NAME,
    controllerPort:       parseInt(process.env.APPDYNAMICS_CONTROLLER_PORT || '443'),
    controllerSslEnabled: process.env.APPDYNAMICS_CONTROLLER_SSL_ENABLED !== 'false',
    accountName:          process.env.APPDYNAMICS_AGENT_ACCOUNT_NAME,
    accountAccessKey:     process.env.APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY,
    applicationName:      process.env.APPDYNAMICS_AGENT_APPLICATION_NAME || 'obs-lab',
    tierName:             process.env.APPDYNAMICS_AGENT_TIER_NAME || 'unknown-tier',
    nodeName:             process.env.APPDYNAMICS_AGENT_NODE_NAME || 'node-1',
  });
  console.log('[appdynamics] Agente iniciado tier: ' + process.env.APPDYNAMICS_AGENT_TIER_NAME);
} catch (err) {
  console.error('[appdynamics] Falha:', err.message);
}
