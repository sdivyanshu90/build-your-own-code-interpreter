/**
 * GET /v1/languages — list the supported runtimes and their metadata.
 *
 * Public (no auth) so clients can discover capabilities before authenticating.
 */
import { Router } from 'express';
import type { Request, Response } from 'express';
import { listRuntimes } from '../languages.js';

export const languagesRouter = Router();

/**
 * @openapi
 * /v1/languages:
 *   get:
 *     summary: List supported language runtimes.
 *     responses:
 *       '200':
 *         description: The list of runtimes.
 *         content:
 *           application/json:
 *             schema:
 *               type: object
 *               properties:
 *                 languages:
 *                   type: array
 *                   items: { $ref: '#/components/schemas/RuntimeInfo' }
 */
languagesRouter.get('/languages', (_req: Request, res: Response) => {
  res.status(200).json({ languages: listRuntimes() });
});
