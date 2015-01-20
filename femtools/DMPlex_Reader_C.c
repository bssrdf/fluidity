#include "DMPlex_Reader_C.h"

PetscErrorCode dmplex_mark_halo_regions(DM *plex)
{
  DMLabel         lblDepth, lblHalo;
  PetscInt        p, pStart, pEnd, cStart, cEnd, nleaves, nroots, npoints;
  PetscInt        a, adjSize, *adj = NULL, cl, clSize, *closure = NULL;
  const PetscInt *ilocal, *points;
  PetscSF         pointSF;
  IS              haloPoints;
  PetscBool       hasValue, useCone;
  PetscErrorCode  ierr;

  PetscFunctionBegin;
  ierr = DMPlexGetChart(*plex, &pStart, &pEnd);CHKERRQ(ierr);
  ierr = DMPlexGetHeightStratum(*plex, 0,  &cStart, &cEnd);CHKERRQ(ierr);
  /* Use star(closure(p)) adjacency to get neighbouring cells */
  ierr = DMPlexGetAdjacencyUseCone(*plex, &useCone);CHKERRQ(ierr);
  ierr = DMPlexSetAdjacencyUseCone(*plex, PETSC_TRUE);CHKERRQ(ierr);

  /* Create labels for L1 and L2 halos */
  ierr = DMPlexGetLabel(*plex, "depth", &lblDepth);CHKERRQ(ierr);
  ierr = DMPlexCreateLabel(*plex, "HaloRegions");CHKERRQ(ierr);
  ierr = DMPlexGetLabel(*plex, "HaloRegions", &lblHalo);CHKERRQ(ierr);
  ierr = DMLabelCreateIndex(lblHalo, pStart, pEnd);CHKERRQ(ierr);

  /* Loop over point SF and mark the entire halo region L2 */
  ierr = DMGetPointSF(*plex, &pointSF);CHKERRQ(ierr);
  ierr = PetscSFGetGraph(pointSF, &nroots, &nleaves, &ilocal, NULL);CHKERRQ(ierr);
  for (p = 0; p < nleaves; p++) {ierr = DMLabelSetValue(lblHalo, ilocal[p], 2);CHKERRQ(ierr);}

  /* Loop over halo cells and test for non-halo neighbouring cells */
  ierr = DMLabelGetStratumIS(lblHalo, 2, &haloPoints);CHKERRQ(ierr);
  ierr = DMLabelGetStratumSize(lblHalo, 2, &npoints);CHKERRQ(ierr);
  ierr = ISGetIndices(haloPoints, &points);CHKERRQ(ierr);
  for (p = 0; p < npoints; p++) {
    const PetscInt cell = points[p];
    if (cStart <= cell && cell < cEnd) {
      adjSize = PETSC_DETERMINE;
      ierr = DMPlexGetAdjacency(*plex, cell, &adjSize, &adj);CHKERRQ(ierr);
      for (a = 0; a < adjSize; ++a) {
        const PetscInt neigh = adj[a];
        if (neigh != cell && cStart <= neigh && neigh < cEnd) {
          /* If the neighbouring cell is not in L2; mark this cell as L1 */
          ierr = DMLabelStratumHasPoint(lblHalo, 2, neigh, &hasValue);CHKERRQ(ierr);
          if (!hasValue) {
            ierr = DMPlexGetTransitiveClosure(*plex, cell, PETSC_TRUE, &clSize, &closure);CHKERRQ(ierr);
            for (cl = 0; cl < 2*clSize; cl+=2) {
              /* L1 is a subset of L2 */
              ierr = DMLabelStratumHasPoint(lblHalo, 2, closure[cl], &hasValue);CHKERRQ(ierr);
              if (hasValue) {ierr = DMLabelSetValue(lblHalo, closure[cl], 1);CHKERRQ(ierr);}
            }
          }
        }
      }
    }
  }
  ierr = ISRestoreIndices(haloPoints, &points);CHKERRQ(ierr);
  ierr = ISDestroy(&haloPoints);CHKERRQ(ierr);
  if (adj) {ierr = PetscFree(adj);CHKERRQ(ierr);}
  if (closure) {ierr = DMPlexRestoreTransitiveClosure(*plex, 0, PETSC_TRUE, &clSize, &closure);CHKERRQ(ierr);}

  ierr = DMPlexSetAdjacencyUseCone(*plex, useCone);CHKERRQ(ierr);
  PetscFunctionReturn(0);
}

PetscErrorCode dmplex_get_point_renumbering(DM *plex, PetscInt depth, IS *renumbering)
{
  MPI_Comm        comm;
  PetscInt        v, p, pStart, pEnd, npoints, idx, *permutation;
  DMLabel         lblHalo;
  IS              haloL1, haloL2;
  const PetscInt *points;
  PetscBool       hasPoint;
  PetscErrorCode  ierr;

  PetscFunctionBegin;
  ierr = PetscObjectGetComm((PetscObject) *plex, &comm);CHKERRQ(ierr);
  ierr = DMPlexGetLabel(*plex, "HaloRegions", &lblHalo);CHKERRQ(ierr);
  ierr = DMPlexGetDepthStratum(*plex, depth, &pStart, &pEnd);CHKERRQ(ierr);

  if (!lblHalo) {
    ierr = ISCreateStride(comm, pEnd-pStart, 0, 1, renumbering);CHKERRQ(ierr);
    PetscFunctionReturn(0);
  }

  /* Add owned points first */
  ierr = PetscMalloc1(pEnd - pStart, &permutation);CHKERRQ(ierr);
  for (idx = 0, v = pStart; v < pEnd; v++) {
    ierr = DMLabelHasPoint(lblHalo, v, &hasPoint);CHKERRQ(ierr);
    if (!hasPoint) permutation[v - pStart] = idx++;
  }

  /* Add entities in L1 halo region */
  ierr = DMLabelGetStratumIS(lblHalo, 1, &haloL1);CHKERRQ(ierr);
  ierr = DMLabelGetStratumSize(lblHalo, 1, &npoints);CHKERRQ(ierr);
  ierr = ISGetIndices(haloL1, &points);CHKERRQ(ierr);
  for (p = 0; p < npoints; p++) {
    if (pStart <= points[p] && points[p] < pEnd) permutation[points[p] - pStart] = idx++;
  }
  ierr = ISRestoreIndices(haloL1, &points);CHKERRQ(ierr);
  ierr = ISDestroy(&haloL1);CHKERRQ(ierr);

  /* Add entities in L2 halo region, but not already in L1 */
  ierr = DMLabelGetStratumIS(lblHalo, 2, &haloL2);CHKERRQ(ierr);
  ierr = DMLabelGetStratumSize(lblHalo, 2, &npoints);CHKERRQ(ierr);
  if (npoints > 0) {
    ierr = ISGetIndices(haloL2, &points);CHKERRQ(ierr);
    for (p = 0; p < npoints; p++) {
      ierr = DMLabelStratumHasPoint(lblHalo, 1, points[p], &hasPoint);CHKERRQ(ierr);
      if (hasPoint) continue;
      if (pStart <= points[p] && points[p] < pEnd) permutation[points[p] - pStart] = idx++;
    }
    ierr = ISRestoreIndices(haloL2, &points);CHKERRQ(ierr);
  }
  ierr = ISDestroy(&haloL2);CHKERRQ(ierr);

  ierr = ISCreateGeneral(comm, pEnd-pStart, permutation, PETSC_OWN_POINTER, renumbering);CHKERRQ(ierr);

  PetscFunctionReturn(0);
}

PetscErrorCode dmplex_get_mesh_connectivity(DM plex, PetscInt nnodes, PetscInt loc,
                                            IS *rnbrCells, IS *rnbrVertices, PetscInt *ndglno)
{
  PetscInt c, cStart, cEnd, vStart, vEnd, idx, ci, nclosure, point;
  PetscInt *closure=NULL;
  const PetscInt *cells, *vertices;
  PetscErrorCode ierr;

  PetscFunctionBegin;
  ierr = DMPlexGetHeightStratum(plex, 0, &cStart, &cEnd);CHKERRQ(ierr);
  ierr = DMPlexGetDepthStratum(plex, 0, &vStart, &vEnd);CHKERRQ(ierr);
  ierr = ISGetIndices(*rnbrCells, &cells);CHKERRQ(ierr);
  ierr = ISGetIndices(*rnbrVertices, &vertices);CHKERRQ(ierr);
  for (c=cStart; c<cEnd; c++) {
    ierr = DMPlexGetTransitiveClosure(plex, c, PETSC_TRUE, &nclosure, &closure);CHKERRQ(ierr);
    for (idx=0, ci=0; ci<nclosure; ci++) {
      point = closure[2*ci];
      if (vStart <= point && point < vEnd) ndglno[loc*cells[c] + idx++] = vertices[point - vStart] + 1;
    }
  }
  if (closure) {
    ierr = DMPlexRestoreTransitiveClosure(plex, c, PETSC_TRUE, &nclosure, &closure);CHKERRQ(ierr);
  }
  ierr = ISRestoreIndices(*rnbrCells, &cells);CHKERRQ(ierr);
  ierr = ISRestoreIndices(*rnbrVertices, &vertices);CHKERRQ(ierr);
  PetscFunctionReturn(0);
}

PetscErrorCode dmplex_get_num_surface_facets(DM plex, const char label_name[], PetscInt *nfacets)
{
  PetscInt v, nvalues, npoints;
  const PetscInt *values;
  DMLabel label;
  IS valueIS;
  PetscErrorCode ierr;

  PetscFunctionBegin;
  ierr = DMPlexGetLabel(plex, label_name, &label);CHKERRQ(ierr);
  ierr = DMLabelGetNumValues(label, &nvalues);CHKERRQ(ierr);
  ierr = DMLabelGetValueIS(label, &valueIS);CHKERRQ(ierr);
  ierr = ISGetIndices(valueIS, &values);CHKERRQ(ierr);
  for (*nfacets=0, v=0; v<nvalues; v++) {
    ierr = DMLabelGetStratumSize(label, values[v], &npoints);CHKERRQ(ierr);
    *nfacets += npoints;
  }
  ierr = ISRestoreIndices(valueIS, &values);CHKERRQ(ierr);
  ierr = ISDestroy(&valueIS);CHKERRQ(ierr);
  PetscFunctionReturn(0);
}

PetscErrorCode dmplex_get_surface_connectivity(DM plex, const char label_name[], PetscInt nfacets, PetscInt sloc, IS *rnbrVertices, PetscInt *sndglno, PetscInt *boundary_ids)
{
  PetscInt        v, vStart, vEnd, nvalues, p, npoints, idx, ci, nclosure, vertex, nvertices;
  const PetscInt *values, *points, *vertices;
  DMLabel         label;
  IS              valueIS, pointIS;
  PetscInt       *closure = NULL;
  PetscErrorCode  ierr;

  PetscFunctionBegin;
  ierr = DMPlexGetDepthStratum(plex, 0, &vStart, &vEnd);CHKERRQ(ierr);
  ierr = DMPlexGetLabel(plex, label_name, &label);CHKERRQ(ierr);
  ierr = DMLabelGetNumValues(label, &nvalues);CHKERRQ(ierr);
  ierr = DMLabelGetValueIS(label, &valueIS);CHKERRQ(ierr);
  ierr = ISGetIndices(valueIS, &values);CHKERRQ(ierr);
  ierr = ISGetIndices(*rnbrVertices, &vertices);CHKERRQ(ierr);
  /* Loop over all marker values in the supplied label */
  for (idx = 0 , v = 0; v < nvalues; v++) {
    ierr = DMLabelGetStratumSize(label, values[v], &npoints);CHKERRQ(ierr);
    ierr = DMLabelGetStratumIS(label, values[v], &pointIS);CHKERRQ(ierr);
    ierr = ISGetIndices(pointIS, &points);CHKERRQ(ierr);
    for (p = 0; p < npoints; p++) {
      /* Derive vertices for each marked facet */
      ierr = DMPlexGetTransitiveClosure(plex, points[p], PETSC_TRUE, &nclosure, &closure);CHKERRQ(ierr);
      for (nvertices = 0, ci = 0; ci < nclosure; ci++) {
        vertex = closure[2*ci];
        if (vStart <= vertex && vertex < vEnd) {
          sndglno[idx*sloc+nvertices++] = vertices[vertex - vStart] + 1;
        }
      }
      /* Store associated boundary ID */
      boundary_ids[idx] = values[v];
      idx++;
    }
    ierr = ISRestoreIndices(pointIS, &points);CHKERRQ(ierr);
    ierr = ISDestroy(&pointIS);CHKERRQ(ierr);
  }
  ierr = ISRestoreIndices(valueIS, &values);CHKERRQ(ierr);
  ierr = ISDestroy(&valueIS);CHKERRQ(ierr);
  if (closure) {
    ierr = DMPlexRestoreTransitiveClosure(plex, points[p], PETSC_TRUE, &nclosure, &closure);CHKERRQ(ierr);
  }
  ierr = ISRestoreIndices(*rnbrVertices, &vertices);CHKERRQ(ierr);
  PetscFunctionReturn(0);
}